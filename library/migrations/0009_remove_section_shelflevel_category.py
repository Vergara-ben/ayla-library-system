import django.db.models.deletion
from django.db import migrations, models


def copy_section_shelf_to_level(apps, schema_editor):
    """Preserve placements: each ShelfLevel adopts its old Section's shelf."""
    ShelfLevel = apps.get_model('library', 'ShelfLevel')
    for level in ShelfLevel.objects.all():
        if level.section_id:
            level.shelf_id = level.section.shelf_id
            level.save(update_fields=['shelf'])


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0008_borrowingrule_transaction_fine_amount'),
    ]

    operations = [
        # 1. New shelf FK on ShelfLevel (nullable for backfill).
        migrations.AddField(
            model_name='shelflevel',
            name='shelf',
            field=models.ForeignKey(
                blank=True, null=True, db_column='shelf_id',
                on_delete=django.db.models.deletion.CASCADE, to='library.shelf',
            ),
        ),
        # 2. label -> category (rename preserves data).
        migrations.RenameField(
            model_name='shelflevel', old_name='label', new_name='category',
        ),
        # 3. Backfill shelf from the soon-to-be-removed section.
        migrations.RunPython(copy_section_shelf_to_level, migrations.RunPython.noop),
        # 4. Make shelf required now that it is populated.
        migrations.AlterField(
            model_name='shelflevel',
            name='shelf',
            field=models.ForeignKey(
                db_column='shelf_id',
                on_delete=django.db.models.deletion.CASCADE, to='library.shelf',
            ),
        ),
        # 5. Drop the section links and the Section model.
        migrations.RemoveField(model_name='shelflevel', name='section'),
        migrations.RemoveField(model_name='book', name='section'),
        migrations.DeleteModel(name='Section'),
    ]
