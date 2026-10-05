"""Donated books: held back from patrons until they are on a shelf, then released."""

from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Book, Donation, InventoryRecord


def release_shelved_donations(book_ids=None):
    """Make every donated book that now has a shelf Available, and mark its donation Shelved."""
    held = Book.objects.filter(status='Donated', shelf_level__isnull=False)
    if book_ids is not None:
        held = held.filter(book_id__in=list(book_ids))
    ids = list(held.values_list('book_id', flat=True))
    if not ids:
        return 0
    Book.objects.filter(book_id__in=ids).update(status='Available')
    (InventoryRecord.objects.filter(book_id__in=ids, source='Donation')
     .exclude(processing_stage='Shelved').update(processing_stage='Shelved'))
    # A donated title is Shelved once none of its copies is still waiting.
    for donation in (Donation.objects.filter(Q(book_id__in=ids) | Q(inventory_copies__book_id__in=ids))
                     .exclude(status='Shelved').distinct()):
        waiting = Book.objects.filter(
            Q(inventory_records__donation=donation) | Q(pk=donation.book_id), status='Donated').exists()
        if not waiting:
            donation.status = 'Shelved'
            donation.save(update_fields=['status'])
    return len(ids)


@receiver(post_save, sender=Book)
def _release_on_save(sender, instance, **kwargs):
    if instance.status == 'Donated' and instance.shelf_level_id:
        release_shelved_donations([instance.book_id])
