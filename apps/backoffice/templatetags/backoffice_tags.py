from django import template
from django.utils import timezone

from apps.backoffice import reports

register = template.Library()


@register.simple_tag
def admin_home_summary():
    """Counts and today's figures for the admin home page."""
    today = timezone.localdate()
    return {"queues": reports.ops_queues(), "today": reports.period_summary(today, today)}
