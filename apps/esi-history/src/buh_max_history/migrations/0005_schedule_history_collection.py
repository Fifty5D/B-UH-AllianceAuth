"""Enable one bounded collector timer when the approved app update is installed."""

from django.db import migrations


def schedule(apps, schema_editor):
    Interval = apps.get_model("django_celery_beat", "IntervalSchedule")
    Task = apps.get_model("django_celery_beat", "PeriodicTask")
    interval, _ = Interval.objects.get_or_create(every=15, period="minutes")
    Task.objects.get_or_create(
        name="B-UH history collection",
        defaults={
            "task": "buh_max_history.tasks.scheduled_history_collection",
            "interval_id": interval.pk,
            "enabled": True,
            "priority": 8,
        },
    )


def unschedule(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(
        name="B-UH history collection",
        task="buh_max_history.tasks.scheduled_history_collection",
    ).update(enabled=False)


class Migration(migrations.Migration):
    dependencies = [
        ("buh_max_history", "0004_archivecollectiontarget_and_more"),
        ("django_celery_beat", "0018_improve_crontab_helptext"),
    ]
    operations = [migrations.RunPython(schedule, unschedule)]
