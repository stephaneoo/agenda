"""The "Modérateurs" group: what a moderator needs, nothing on users.

View and change on events and occurrences (change is what the "Modérer" page asks; the
canonical rows themselves stay read-only), view on sources and sync runs.
"""

from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP = "Modérateurs"
PERMISSIONS = [
    ("agenda", "view_event"), ("agenda", "change_event"),
    ("agenda", "view_occurrence"), ("agenda", "change_occurrence"),
    ("claims", "view_source"),
    ("ingest", "view_syncrun"),
]


def create_group(apps, schema_editor):
    # Permissions are normally created after migrate (post_migrate): make sure they exist.
    for label in {app for app, _ in PERMISSIONS}:
        config = apps.get_app_config(label)
        config.models_module = True
        create_permissions(config, apps=apps, verbosity=0)
        config.models_module = None
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=GROUP)
    for app, codename in PERMISSIONS:
        group.permissions.add(Permission.objects.get(content_type__app_label=app, codename=codename))


def delete_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GROUP).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("agenda", "0002_review_guards_and_end_index"),
        ("claims", "0004_claim_guard_event_key_check"),
        ("ingest", "0002_review_refused_warning"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(create_group, delete_group)]
