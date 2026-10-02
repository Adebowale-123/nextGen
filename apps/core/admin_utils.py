from django.contrib import admin
from django.contrib.admin import helpers
from django.template.response import TemplateResponse


def reason_action(description, handler, *, label="Reason"):
    """Build an admin action that asks for a free-text reason before running.

    `handler(modeladmin, request, obj, reason)` is called once per selected object.
    """

    @admin.action(description=description)
    def action(modeladmin, request, queryset):
        if request.POST.get("apply"):
            reason = request.POST.get("reason", "").strip()
            if not reason:
                modeladmin.message_user(request, "A reason is required.", level="error")
                return None
            done = 0
            for obj in queryset:
                try:
                    handler(modeladmin, request, obj, reason)
                    done += 1
                except Exception as exc:  # report per-object failures without aborting the batch
                    modeladmin.message_user(request, f"{obj}: {exc}", level="error")
            modeladmin.message_user(request, f"{description}: {done} done.")
            return None
        return TemplateResponse(request, "admin/reason_form.html", {
            **modeladmin.admin_site.each_context(request),
            "title": description,
            "objects": queryset,
            "action": request.POST.get("action"),
            "label": label,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "opts": modeladmin.model._meta,
        })

    action.__name__ = handler.__name__ + "_action"
    return action


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
