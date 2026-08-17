"""Ask a Librarian — the enquiry desk between patrons and library staff.

Not instant messaging. Ayla has one computer and one librarian, who cannot sit
in a live chat while also working the front desk, so this is shaped like a help
desk: a patron asks a question, gets on with their day, and is emailed when
somebody answers. The librarian works a queue of threads still waiting for a
reply rather than watching for a notification.

Both sides poll rather than hold a socket open — the deployment target has no
WebSocket support, and a page that refreshes every few seconds is enough for a
conversation measured in hours.
"""

from datetime import timedelta

from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .audit import log_admin_action
from .auth_utils import (admin_login_required, granted_module_required,
                         patron_login_required)
from .emails import librarian_reply_email
from .models import ChatMessage, Conversation, Patron, User

# A patron reading the thread right now does not need an email about it, and a
# librarian typing three short answers in a row should not send three.
ACTIVE_WINDOW = timedelta(minutes=2)
NOTIFY_COOLDOWN = timedelta(minutes=10)

MAX_MESSAGE_LENGTH = 2000


def _serialise(message):
    return {
        'id': message.message_id,
        'sender': message.sender_type,
        'staff': message.staff.fullname if message.staff else None,
        'body': message.body,
        'sent_at': timezone.localtime(message.sent_at).strftime('%b %d, %Y · %I:%M %p'),
        'read': message.read_at is not None,
    }


def _thread_payload(conversation, for_staff):
    messages = list(conversation.messages.select_related('staff').all())
    return {
        'success': True,
        'conversation_id': conversation.conversation_id,
        'status': conversation.status,
        'status_label': conversation.get_status_display(),
        'topic': conversation.topic,
        'messages': [_serialise(m) for m in messages],
        'patron': conversation.patron.fullname if for_staff else None,
    }


# ─── patron side ──────────────────────────────────────────────────────────

@patron_login_required
def patron_messages(request):
    """The patron's own thread with the library."""
    patron = Patron.objects.filter(patron_id=request.session.get('patron_id')).first()
    if patron is None:
        return redirect('/patron/login/')

    conversation = (Conversation.objects
                    .filter(patron=patron)
                    .exclude(status='Closed')
                    .order_by('-last_message_at')
                    .first())
    history = (Conversation.objects
               .filter(patron=patron, status='Closed')
               .order_by('-last_message_at')[:10])

    return render(request, 'patron/messages.html', {
        'patron': patron,
        'conversation': conversation,
        'messages': list(conversation.messages.select_related('staff').all())
                    if conversation else [],
        'history': history,
        'topic_choices': Conversation.TOPIC_CHOICES,
    })


@patron_login_required
def patron_send_message(request):
    """Ask a question, or add to the thread already open."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})

    patron = Patron.objects.filter(patron_id=request.session.get('patron_id')).first()
    if patron is None:
        return JsonResponse({'success': False, 'error': 'Please sign in again.'})

    body = (request.POST.get('body') or '').strip()
    if not body:
        return JsonResponse({'success': False, 'error': 'Type your message first.'})
    if len(body) > MAX_MESSAGE_LENGTH:
        return JsonResponse({'success': False,
                             'error': 'That message is too long — please shorten it.'})

    topic = (request.POST.get('topic') or 'Other').strip()
    if topic not in dict(Conversation.TOPIC_CHOICES):
        topic = 'Other'

    conversation = (Conversation.objects
                    .filter(patron=patron)
                    .exclude(status='Closed')
                    .order_by('-last_message_at')
                    .first())
    if conversation is None:
        conversation = Conversation.objects.create(patron=patron, topic=topic)

    ChatMessage.objects.create(conversation=conversation, sender_type='Patron', body=body)
    # Anything the patron says puts the thread back in the queue, including a
    # follow-up on something staff thought they had finished.
    conversation.status = 'Open'
    conversation.last_message_at = timezone.now()
    # Deliberately does not touch patron_last_seen_at. Sending a question and
    # closing the tab is the normal thing to do, and treating "just sent" as
    # "still watching" would swallow the email for a reply that arrives a
    # minute later — the exact case the email exists for. Presence comes from
    # the poll, which only runs while the page is genuinely open.
    conversation.save(update_fields=['status', 'last_message_at'])

    return JsonResponse(_thread_payload(conversation, for_staff=False))


@patron_login_required
def patron_poll_messages(request):
    """Refresh the thread, and note that the patron is looking at it.

    That last part is what stops a reply typed while they are reading from
    also arriving as an email a second later.
    """
    patron = Patron.objects.filter(patron_id=request.session.get('patron_id')).first()
    if patron is None:
        return JsonResponse({'success': False, 'error': 'Please sign in again.'})

    conversation = (Conversation.objects
                    .filter(patron=patron)
                    .exclude(status='Closed')
                    .order_by('-last_message_at')
                    .first())
    if conversation is None:
        return JsonResponse({'success': True, 'conversation_id': None, 'messages': []})

    now = timezone.now()
    conversation.patron_last_seen_at = now
    conversation.save(update_fields=['patron_last_seen_at'])
    conversation.messages.filter(sender_type='Staff', read_at__isnull=True).update(read_at=now)

    return JsonResponse(_thread_payload(conversation, for_staff=False))


def patron_unread_count(patron_id):
    return ChatMessage.objects.filter(
        conversation__patron_id=patron_id,
        sender_type='Staff',
        read_at__isnull=True).count()


# ─── library side ─────────────────────────────────────────────────────────

def _patron_directory(query):
    """Every patron the library can write to, with their thread state attached.

    The list is of *people*, not of conversations, because the librarian also
    needs to start one — telling somebody their reserved book has arrived is
    the same job as answering a question, and a list of existing threads has
    nowhere to do it from.

    Counts are gathered separately rather than annotated across two joins,
    where an unread tally and a conversation count inflate each other.
    """
    patrons = Patron.objects.exclude(account_status='Visitor')
    if query:
        patrons = patrons.filter(Q(fullname__icontains=query) | Q(email__icontains=query))
    patrons = list(patrons.order_by('fullname'))

    unread = dict(ChatMessage.objects
                  .filter(sender_type='Patron', read_at__isnull=True)
                  .values('conversation__patron_id')
                  .annotate(n=Count('message_id'))
                  .values_list('conversation__patron_id', 'n'))

    threads = {}
    for conversation in (Conversation.objects
                         .exclude(status='Closed')
                         .order_by('patron_id', '-last_message_at')):
        threads.setdefault(conversation.patron_id, conversation)

    last_seen = dict(Conversation.objects
                     .values('patron_id')
                     .annotate(last=Max('last_message_at'))
                     .values_list('patron_id', 'last'))

    latest_body = {}
    for message in (ChatMessage.objects
                    .select_related('conversation')
                    .order_by('conversation__patron_id', '-sent_at')):
        latest_body.setdefault(message.conversation.patron_id,
                               (message.sender_type, message.body))

    for patron in patrons:
        conversation = threads.get(patron.patron_id)
        patron.thread = conversation
        patron.unread = unread.get(patron.patron_id, 0)
        patron.waiting = bool(conversation and conversation.status == 'Open')
        patron.last_activity = last_seen.get(patron.patron_id)
        sender, body = latest_body.get(patron.patron_id, (None, ''))
        patron.preview = (('You: ' if sender == 'Staff' else '') + body) if body else ''

    # Unanswered questions first, then whoever spoke most recently, then the
    # rest of the directory — so the queue stays on top without a tab to find it.
    patrons.sort(key=lambda p: (
        0 if p.waiting else 1,
        0 if p.unread else 1,
        -(p.last_activity.timestamp()) if p.last_activity else 0,
        p.fullname.lower(),
    ))
    return patrons


def _staff_messages_page(request, template):
    """One list of everybody, with the unanswered questions floated to the top."""
    query = (request.GET.get('q') or '').strip()
    patrons = _patron_directory(query)

    selected_patron = None
    raw_id = (request.GET.get('p') or '').strip()
    if raw_id.isdigit():
        selected_patron = Patron.objects.filter(patron_id=int(raw_id)).first()
    if selected_patron is None and patrons:
        selected_patron = patrons[0]

    selected = None
    thread = []
    if selected_patron is not None:
        selected = (Conversation.objects
                    .filter(patron=selected_patron)
                    .exclude(status='Closed')
                    .order_by('-last_message_at')
                    .first())
        if selected is None:
            selected = (Conversation.objects.filter(patron=selected_patron)
                        .order_by('-last_message_at').first())
        if selected is not None:
            thread = list(selected.messages.select_related('staff').all())
            # Opening a thread is reading it.
            selected.messages.filter(sender_type='Patron', read_at__isnull=True).update(
                read_at=timezone.now())

    return render(request, template, {
        'patrons': patrons,
        'selected_patron': selected_patron,
        'selected': selected,
        'thread': thread,
        'query': query,
        'open_count': Conversation.objects.filter(status='Open').count(),
    })


@granted_module_required('chat')
def admin_messages(request):
    template = ('library_staff/messages.html'
                if request.session.get('admin_role') == 'Staff'
                else 'admin/messages.html')
    return _staff_messages_page(request, template)


@granted_module_required('chat')
def staff_reply(request):
    """Answer a patron, and let them know an answer is waiting."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})

    body = (request.POST.get('body') or '').strip()
    if not body:
        return JsonResponse({'success': False, 'error': 'Type a message first.'})
    if len(body) > MAX_MESSAGE_LENGTH:
        return JsonResponse({'success': False, 'error': 'That message is too long.'})

    raw_id = (request.POST.get('conversation_id') or '').strip()
    conversation = (Conversation.objects.select_related('patron')
                    .filter(conversation_id=raw_id).first() if raw_id.isdigit() else None)

    if conversation is None or conversation.status == 'Closed':
        # Writing to somebody who has never asked anything — a held book, an
        # overdue notice — opens the thread rather than refusing.
        raw_patron = (request.POST.get('patron_id') or '').strip()
        patron = (Patron.objects.filter(patron_id=int(raw_patron)).first()
                  if raw_patron.isdigit() else None)
        if patron is None:
            return JsonResponse({'success': False, 'error': 'Pick a patron to write to.'})
        if patron.account_status == 'Visitor':
            return JsonResponse({'success': False,
                                 'error': 'Visitors have no account to receive messages.'})
        conversation = Conversation.objects.create(patron=patron, topic='Other')

    staff = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    ChatMessage.objects.create(conversation=conversation, sender_type='Staff',
                               staff=staff, body=body)

    now = timezone.now()
    # Nothing is waiting on the library once it has spoken, whether that was an
    # answer or the first word.
    conversation.status = 'Answered'
    conversation.last_message_at = now
    conversation.save(update_fields=['status', 'last_message_at'])

    emailed = _notify_patron(conversation, body, now)
    return JsonResponse(dict(_thread_payload(conversation, for_staff=True), emailed=emailed))


def _notify_patron(conversation, body, now):
    """Email the patron that a reply is waiting, when that is actually useful.

    Skipped while they have the thread open, since the reply is already on
    their screen, and rate-limited so a librarian answering in three short
    messages does not send three emails a minute apart.
    """
    patron = conversation.patron
    if not patron.email:
        return False

    seen = conversation.patron_last_seen_at
    if seen and (now - seen) < ACTIVE_WINDOW:
        return False
    last = conversation.last_notified_at
    if last and (now - last) < NOTIFY_COOLDOWN:
        return False

    if not librarian_reply_email(patron, body):
        return False
    conversation.last_notified_at = now
    conversation.save(update_fields=['last_notified_at'])
    return True


@granted_module_required('chat')
def close_conversation(request):
    """Mark an enquiry finished. A new question from the patron reopens it."""
    if request.method != 'POST':
        return redirect('admin_messages')

    raw_id = (request.POST.get('conversation_id') or '').strip()
    conversation = (Conversation.objects.select_related('patron')
                    .filter(conversation_id=raw_id).first() if raw_id.isdigit() else None)
    if conversation is None:
        return redirect('admin_messages')

    conversation.status = 'Closed'
    conversation.closed_at = timezone.now()
    conversation.closed_by = User.objects.filter(
        admin_id=request.session.get('admin_id')).first()
    conversation.save(update_fields=['status', 'closed_at', 'closed_by'])
    log_admin_action(request, 'Update', 'Conversation', conversation.conversation_id,
                     'Closed the enquiry from "' + conversation.patron.fullname + '"')
    return redirect('/admin-portal/messages/?p=' + str(conversation.patron_id))


@admin_login_required
def staff_poll_messages(request):
    """New patron messages for the thread on screen, plus the queue size."""
    raw_id = (request.GET.get('conversation_id') or '').strip()
    payload = {'success': True,
               'open_count': Conversation.objects.filter(status='Open').count()}
    if raw_id.isdigit():
        conversation = Conversation.objects.filter(conversation_id=int(raw_id)).first()
        if conversation is not None:
            conversation.messages.filter(sender_type='Patron', read_at__isnull=True).update(
                read_at=timezone.now())
            payload.update(_thread_payload(conversation, for_staff=True))
    return JsonResponse(payload)
