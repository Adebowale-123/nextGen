from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils import timezone


@login_required
def inbox(request):
    notes = request.user.notifications.all()
    page = Paginator(notes, 20).get_page(request.GET.get("page"))
    response = render(request, "notifications/inbox.html", {"page": page})
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return response
