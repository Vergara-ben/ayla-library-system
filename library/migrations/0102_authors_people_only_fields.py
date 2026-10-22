from django.db import migrations, models


class Migration(migrations.Migration):
    """Authors only: no kind of author, and no role on a book."""

    dependencies = [
        ('library', '0101_authors_people_only'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='bookauthor',
            name='one_credit_per_role',
        ),
        migrations.RemoveField(
            model_name='author',
            name='kind',
        ),
        migrations.RemoveField(
            model_name='bookauthor',
            name='role',
        ),
        migrations.AddConstraint(
            model_name='bookauthor',
            constraint=models.UniqueConstraint(fields=('book', 'author'), name='one_credit_per_author'),
        ),
    ]
