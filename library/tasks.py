"""Endpoints for the hosting setup: a health check and the daily task trigger."""

import hmac
import io
import logging

from django.conf import settings
from django.core.management import call_command
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger('library')


def healthz(request):
    """Plain OK for uptime pings and the host's health check."""
    return HttpResponse('ok', content_type='text/plain')


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def run_daily_task(request):
    """Run daily_maintenance when called with the secret token."""
    expected = getattr(settings, 'DAILY_TASK_TOKEN', '')
    given = request.headers.get('X-Task-Token') or request.GET.get('token') or ''
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        return JsonResponse({'ok': False, 'error': 'Not allowed.'}, status=403)

    out = io.StringIO()
    ok = True
    try:
        call_command('daily_maintenance', stdout=out, stderr=out)
    except SystemExit as exc:
        ok = False
        out.write(str(exc))
    except Exception:
        ok = False
        logger.exception('Daily task failed')
    logger.info('Daily task finished: %s', 'ok' if ok else 'failed')
    return JsonResponse({'ok': ok, 'output': out.getvalue()[-4000:]}, status=200 if ok else 500)
