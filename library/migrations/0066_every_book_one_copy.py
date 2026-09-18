from uuid import uuid4

from django.db import migrations, models
from django.utils import timezone


def _book_condition(copy_condition, fallback='Good'):
    """The catalogue condition that matches an inventory condition."""
    return 'Damaged' if copy_condition == 'Damaged' else fallback


def give_every_book_one_copy(apps, schema_editor):
    """One physical copy is one book record with one inventory copy."""
    Book = apps.get_model('library', 'Book')
    InventoryRecord = apps.get_model('library', 'InventoryRecord')
    StockMovement = apps.get_model('library', 'StockMovement')
    now = timezone.now()

    # The base manager, so archived books are seen when this runs outside a migration.
    books = Book._base_manager
    used_codes = set(books.exclude(qr_code__isnull=True).exclude(qr_code='')
                     .values_list('qr_code', flat=True))
    used_labels = set(InventoryRecord.objects.exclude(qr_label__isnull=True)
                      .values_list('qr_label', flat=True))

    # Copies received together were filed under one book record; give each its own.
    shared = (InventoryRecord.objects.filter(book__isnull=False)
              .values('book_id').annotate(n=models.Count('inventory_id')).filter(n__gt=1))
    for row in shared:
        book = books.get(book_id=row['book_id'])
        records = list(InventoryRecord.objects.filter(book_id=book.book_id).order_by('inventory_id'))
        for record in records[1:]:
            # The label already stuck on this copy becomes its book QR.
            code = record.qr_label if record.qr_label and record.qr_label not in used_codes else str(uuid4())
            used_codes.add(code)
            if record.status == 'Missing':
                status = 'Missing'
            elif record.condition == 'Lost':
                status = 'Lost'
            elif record.source == 'Donation' and record.processing_stage != 'Shelved':
                status = 'Donated'
            else:
                status = 'Available'
            removed = record.status == 'Removed'
            clone = books.create(
                title=book.title,
                author=book.author,
                publication_year=book.publication_year,
                genre=book.genre,
                material_type=book.material_type,
                call_number=book.call_number,
                cover_img_url=book.cover_img_url,
                condition=_book_condition(record.condition, book.condition if book.condition != 'Damaged' else 'Good'),
                status=status,
                qr_code=code,
                shelf_level_id=book.shelf_level_id,
                missing_since=record.missing_since if status == 'Missing' else None,
                audit_misses=record.audit_misses if status == 'Missing' else 0,
                archived_at=book.archived_at or (now if removed else None),
                archived_by=book.archived_by or ('System' if removed else ''),
                archive_reason=book.archive_reason or ('Removed from inventory' if removed else ''),
            )
            record.book_id = clone.book_id
            record.save(update_fields=['book'])

    # Books catalogued in Manage Books were never received; count them into stock.
    held = set(InventoryRecord.objects.filter(book__isnull=False).values_list('book_id', flat=True))
    for book in books.filter(archived_at__isnull=True).order_by('book_id'):
        if book.book_id in held:
            continue
        if not book.qr_code:
            book.qr_code = str(uuid4())
            book.save(update_fields=['qr_code'])
        label = book.qr_code if book.qr_code not in used_labels else str(uuid4())
        used_labels.add(label)
        condition = 'Lost' if book.status == 'Lost' else _book_condition(book.condition)
        missing = book.status == 'Missing'
        record = InventoryRecord.objects.create(
            book_id=book.book_id,
            source='Existing',
            condition=condition,
            status='Missing' if missing else 'In Stock',
            qr_label=label,
            missing_since=book.missing_since if missing else None,
            audit_misses=book.audit_misses if missing else 0,
        )
        StockMovement.objects.create(
            inventory_record=record,
            action='ExistingStock',
            actor_name='System',
            reason='Counted into stock from the existing collection',
            source='Existing collection',
            condition_after=condition,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0065_library_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='inventoryrecord',
            name='source',
            field=models.CharField(choices=[('Purchase', 'Shipment'), ('Donation', 'Donation'), ('Existing', 'Existing collection')], default='Purchase', max_length=20),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='action',
            field=models.CharField(choices=[('Received', 'Received'), ('ConditionChange', 'Condition Change'), ('AuditAdjustment', 'Audit Adjustment'), ('Found', 'Found During Audit'), ('Correction', 'Correction'), ('Deaccession', 'Deaccession'), ('ExistingStock', 'Existing Stock')], max_length=30),
        ),
        migrations.RunPython(give_every_book_one_copy, migrations.RunPython.noop),
    ]
