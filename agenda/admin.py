"""The moderation admin (spec, Admin).

Read-only over the canonical tables: they are a projection of the claims, and their models
refuse any other write. A moderator acts through the "Modérer" page of an event or an
occurrence, whose forms call agenda.moderation (moderation claims only). Each change page
shows the claims behind it: every field with its source, value and date, and which one wins.

Claim and ClaimHistory are never registered (claims/admin.py). Source is editable (label,
priority, licence, external); saving it reprojects every event, after the admin's
transaction has committed, one event per transaction (agenda.projection.rebuild_all).
"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import partial
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from agenda import moderation
from agenda.categories import SLUGS
from agenda.fields import MAX_TEXT, PER_SOURCE, problem
from agenda.models import Event, Occurrence
from agenda.projection import rebuild_all
from claims.models import Claim, Source
from claims.resolve import resolve

STATUS_LABELS = {"postponed": "Reporté", "cancelled": "Annulé", "scheduled": "Prévu (valeur de la source)"}
JSON_FIELDS = ("location", "organizer", "offers")
MAX_SHOWN_FAILURES = 50


# --- Claims panel ----------------------------------------------------------------------------

def claims_panel(event_key: str, only: str | None = None) -> list[dict[str, Any]]:
    """The claims of an event (or of one of its targets), grouped by target, each marked with
    whether it wins (None for per-source fields, which are all kept)."""
    rows = Claim.objects.filter(event_key=event_key).select_related("source").order_by("target_key", "field", "-seen_at")
    by_target: dict[str, list[Claim]] = defaultdict(list)
    for c in rows:
        if only is None or c.target_key == only:
            by_target[c.target_key].append(c)
    groups = []
    for target in sorted(by_target, key=lambda k: (k != event_key, k)):
        claims = by_target[target]
        valid = [c for c in claims if c.field == "visibility" or problem(c.target_type, c.target_key, c.field, c.value) is None]
        winners = resolve(c for c in valid if c.field not in PER_SOURCE)
        groups.append({
            "target": target,
            "label": "Événement" if target == event_key else f"Occurrence du {target.rsplit('/', 1)[-1]}",
            "rows": [{
                "field": c.field,
                "value": json.dumps(c.value, ensure_ascii=False)[:500],
                "source": c.source.label,
                "seen_at": c.seen_at,
                "wins": None if c.field in PER_SOURCE else (c in valid and winners.get(c.field) is not None
                                                            and winners[c.field].source == c.source_id),
            } for c in claims],
        })
    return groups


# --- Forms -----------------------------------------------------------------------------------

REASON_ERRORS = {"required": "Le motif est obligatoire."}


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Motif", max_length=MAX_TEXT, error_messages=REASON_ERRORS)


class EditForm(forms.Form):
    field = forms.ChoiceField(label="Champ", choices=[(f, f) for f in moderation.EDITABLE_FIELDS])
    value = forms.CharField(
        label="Valeur", required=False, widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Texte pour name et description ; un slug pour category "
        f"({', '.join(SLUGS)}) ; du JSON pour {', '.join(JSON_FIELDS)} (null pour aucun).",
    )
    revert = forms.BooleanField(label="Annuler la modification (revenir aux sources)", required=False)

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        field, raw = data.get("field"), data.get("value") or ""
        if field in JSON_FIELDS and not data.get("revert"):
            try:
                data["parsed"] = json.loads(raw) if raw.strip() else None
            except ValueError:
                raise forms.ValidationError("La valeur n'est pas du JSON valide.") from None
        else:
            data["parsed"] = raw.strip() or None
        return data


class StatusForm(forms.Form):
    status = forms.ChoiceField(label="Statut", choices=list(STATUS_LABELS.items()))


class MoveForm(forms.Form):
    new_date = forms.DateField(label="Nouvelle date", widget=forms.DateInput(attrs={"type": "date"}))
    new_time = forms.TimeField(label="Nouvelle heure (vide : la même)", required=False,
                               widget=forms.TimeInput(attrs={"type": "time"}))


# --- Admins ----------------------------------------------------------------------------------

class ModeratedAdmin(admin.ModelAdmin):
    """Read-only canonical rows, a claims panel, and a moderation page."""

    change_form_template = "admin/agenda/moderated_change_form.html"
    actions = None
    # action -> (label, form class), set by subclasses.
    moderation_forms: dict[str, tuple[str, type[forms.Form]]] = {}

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False  # the projection writes these rows; moderators write claims

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def can_moderate(self, request: HttpRequest) -> bool:
        opts = self.model._meta
        user = request.user
        return user.is_active and user.is_staff and user.has_perm(f"{opts.app_label}.change_{opts.model_name}")

    def get_urls(self) -> list:
        opts = self.model._meta
        return [
            path("<path:object_id>/moderer/", self.admin_site.admin_view(self.moderate_view),
                 name=f"{opts.app_label}_{opts.model_name}_moderate"),
            *super().get_urls(),
        ]

    def event_key(self, obj: Any) -> str:
        raise NotImplementedError

    def panel_target(self, obj: Any) -> str | None:
        return None

    def change_view(self, request: HttpRequest, object_id: str, form_url: str = "", extra_context: dict | None = None):
        obj = self.get_object(request, object_id)
        extra = dict(extra_context or {})
        if obj is not None:
            extra["claims_groups"] = claims_panel(self.event_key(obj), self.panel_target(obj))
            if self.can_moderate(request):
                opts = self.model._meta
                extra["moderate_url"] = reverse(f"admin:{opts.app_label}_{opts.model_name}_moderate", args=[obj.pk])
        return super().change_view(request, object_id, form_url, extra)

    def apply(self, request: HttpRequest, obj: Any, action: str, data: dict[str, Any]) -> tuple[str, Any]:
        """Run one moderation action; (message for the log, object to show next)."""
        raise NotImplementedError

    def moderate_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404
        if not self.can_moderate(request):
            raise PermissionDenied
        bound: dict[str, forms.Form] = {}
        if request.method == "POST":
            action = request.POST.get("action", "")
            if action not in self.moderation_forms:
                raise PermissionDenied
            form = self.moderation_forms[action][1](request.POST, auto_id=f"id_{action}_%s")
            if form.is_valid():
                try:
                    message, after = self.apply(request, obj, action, form.cleaned_data)
                except ValueError as e:
                    form.add_error(None, str(e))
                else:
                    self.log_change(request, obj, message)
                    messages.success(request, message)
                    opts = after._meta
                    return HttpResponseRedirect(reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[after.pk]))
            bound[action] = form
        opts = self.model._meta
        context = {
            **self.admin_site.each_context(request),
            "opts": opts,
            "original": obj,
            "title": f"Modérer : {obj}",
            "forms": [
                {"action": a, "label": label, "form": bound.get(a) or cls(auto_id=f"id_{a}_%s")}
                for a, (label, cls) in self.moderation_forms.items()
            ],
            "claims_groups": claims_panel(self.event_key(obj), self.panel_target(obj)),
            "change_url": reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk]),
        }
        return TemplateResponse(request, "admin/agenda/moderate.html", context)


class OccurrenceInline(admin.TabularInline):
    model = Occurrence
    fields = ("local_date", "start", "end", "status", "listed", "visibility", "shown")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True
    ordering = ("local_date",)

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Event)
class EventAdmin(ModeratedAdmin):
    list_display = ("name", "commune_name", "category", "visibility", "shown", "updated_at")
    list_filter = ("shown", "visibility", "category")
    search_fields = ("name", "key", "id")
    inlines = [OccurrenceInline]
    moderation_forms = {
        "hide": ("Masquer l'événement", ReasonForm),
        "restore": ("Rétablir (retire le masquage)", ReasonForm),
        "edit": ("Modifier un champ", EditForm),
    }

    @admin.display(description="commune")
    def commune_name(self, obj: Event) -> str:
        return obj.location.get("commune_name") or obj.commune

    def event_key(self, obj: Event) -> str:
        return obj.key

    def apply(self, request: HttpRequest, obj: Event, action: str, data: dict[str, Any]) -> tuple[str, Any]:
        if action == "hide":
            moderation.hide(obj, data["reason"], request.user)
            return f"Masqué : {data['reason']}", obj
        if action == "restore":
            moderation.restore(obj, request.user, data["reason"])
            return f"Rétabli : {data['reason']}", obj
        if data.get("revert"):
            moderation.revert_field(obj, data["field"], request.user)
            return f"Modification de {data['field']} annulée", obj
        moderation.edit_field(obj, data["field"], data["parsed"], request.user)
        return f"{data['field']} modifié", obj


@admin.register(Occurrence)
class OccurrenceAdmin(ModeratedAdmin):
    list_display = ("event", "local_date", "start", "status", "listed", "visibility", "shown")
    list_filter = ("shown", "status", "listed", "visibility")
    search_fields = ("event__name", "key", "id")
    date_hierarchy = "local_date"
    list_select_related = ("event",)
    moderation_forms = {
        "hide": ("Masquer cette occurrence", ReasonForm),
        "restore": ("Rétablir (retire le masquage)", ReasonForm),
        "status": ("Changer le statut", StatusForm),
        "move": ("Déplacer à une autre date", MoveForm),
    }

    def event_key(self, obj: Occurrence) -> str:
        return obj.event.key

    def panel_target(self, obj: Occurrence) -> str:
        return obj.key

    def apply(self, request: HttpRequest, obj: Occurrence, action: str, data: dict[str, Any]) -> tuple[str, Any]:
        if action == "hide":
            moderation.hide(obj, data["reason"], request.user)
            return f"Masquée : {data['reason']}", obj
        if action == "restore":
            moderation.restore(obj, request.user, data["reason"])
            return f"Rétablie : {data['reason']}", obj
        if action == "status":
            moderation.set_status(obj, data["status"], request.user)
            return f"Statut : {STATUS_LABELS[data['status']]}", obj
        moderation.move(obj, data["new_date"], data["new_time"], request.user)
        moved = Occurrence.objects.filter(key=f"{obj.event.key}/{data['new_date'].isoformat()}").first()
        return f"Déplacée au {data['new_date']:%d/%m/%Y}", moved or obj


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("name", "label", "priority", "licence", "external", "last_outcome", "last_success_at")
    fields = ("name", "label", "priority", "licence", "external", "last_run_at", "last_outcome", "last_success_at")
    readonly_fields = ("name", "last_run_at", "last_outcome", "last_success_at")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: Source, form: forms.Form, change: bool) -> None:
        super().save_model(request, obj, form, change)
        if form.changed_data:
            # Source changes don't reproject by themselves. Not inside the admin's own
            # transaction: rebuild_all takes one transaction per event.
            transaction.on_commit(partial(self._rebuild, request))

    def _rebuild(self, request: HttpRequest) -> None:
        result = rebuild_all()
        if result.failed:
            shown = ", ".join(result.failed[:MAX_SHOWN_FAILURES])
            more = f" et {len(result.failed) - MAX_SHOWN_FAILURES} autres" if len(result.failed) > MAX_SHOWN_FAILURES else ""
            messages.error(request, (
                f"Reprojection : {len(result.failed)} événement(s) sur {result.count} en échec, laissés tels "
                f"quels : {shown}{more}. Voir les journaux, puis relancer manage.py rebuild_projection."
            ))
        else:
            messages.success(request, f"Reprojection faite : {result.count} événement(s).")
