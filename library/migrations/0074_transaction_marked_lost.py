from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0073_book_price_no_grace'),
    ]

    operations = [
        migrations.AddField(
            model_name='transaction',
            name='marked_lost',
            field=models.BooleanField(default=False),
        ),
    ]
