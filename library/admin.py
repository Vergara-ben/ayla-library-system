from django.contrib import admin
from .models import (
    FloorPlan, BLEBeacon, Room, Shelf, Waypoint, WaypointConnection,
    Section, ShelfLevel, Book, Donation, User, Announcement,
    Patron, Transaction, PatronLog
)


@admin.register(FloorPlan)
class FloorPlanAdmin(admin.ModelAdmin):
    list_display = ('floor_plan_id', 'is_active', 'renovation_notice', 'uploaded_at')
    list_filter = ('is_active', 'uploaded_at')
    search_fields = ('renovation_notice', 'renovation_message')
    readonly_fields = ('uploaded_at',)


@admin.register(BLEBeacon)
class BLEBeaconAdmin(admin.ModelAdmin):
    list_display = ('beacon_id', 'beacon_uuid', 'label', 'floor_plan', 'map_x', 'map_y')
    list_filter = ('floor_plan',)
    search_fields = ('beacon_uuid', 'label')
    raw_id_fields = ('floor_plan',)


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('room_id', 'name', 'floor_plan', 'map_x', 'map_y')
    list_filter = ('floor_plan',)
    search_fields = ('name', 'description')
    raw_id_fields = ('floor_plan',)


@admin.register(Shelf)
class ShelfAdmin(admin.ModelAdmin):
    list_display = ('shelf_id', 'name', 'room', 'map_x', 'map_y')
    list_filter = ('room',)
    search_fields = ('name', 'description')
    raw_id_fields = ('room',)


@admin.register(Waypoint)
class WaypointAdmin(admin.ModelAdmin):
    list_display = ('waypoint_id', 'label', 'floor_plan', 'map_x', 'map_y', 'linked_shelf')
    list_filter = ('floor_plan',)
    search_fields = ('label',)
    raw_id_fields = ('floor_plan', 'linked_shelf')


@admin.register(WaypointConnection)
class WaypointConnectionAdmin(admin.ModelAdmin):
    list_display = ('connection_id', 'waypoint_from', 'waypoint_to', 'distance')
    raw_id_fields = ('waypoint_from', 'waypoint_to')


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ('section_id', 'name', 'shelf')
    list_filter = ('shelf',)
    search_fields = ('name', 'description')
    raw_id_fields = ('shelf',)


@admin.register(ShelfLevel)
class ShelfLevelAdmin(admin.ModelAdmin):
    list_display = ('shelf_level_id', 'level_number', 'label', 'section')
    list_filter = ('section', 'level_number')
    search_fields = ('label',)
    raw_id_fields = ('section',)


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ('book_id', 'title', 'author', 'ISBN', 'status', 'section', 'shelf_level')
    list_filter = ('status', 'genre', 'section', 'shelf_level')
    search_fields = ('title', 'author', 'ISBN', 'genre')
    raw_id_fields = ('section', 'shelf_level')


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = ('donation_id', 'donor_name', 'book', 'date_donated', 'status')
    list_filter = ('status', 'date_donated')
    search_fields = ('donor_name',)
    raw_id_fields = ('book',)
    date_hierarchy = 'date_donated'


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('admin_id', 'fullname', 'email', 'account_status')
    list_filter = ('account_status',)
    search_fields = ('fullname', 'email')


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('announcement_id', 'title', 'posted_by', 'created_at', 'is_active')
    list_filter = ('is_active', 'created_at')
    search_fields = ('title', 'message')
    raw_id_fields = ('posted_by',)
    date_hierarchy = 'created_at'


@admin.register(Patron)
class PatronAdmin(admin.ModelAdmin):
    list_display = ('patron_id', 'fullname', 'patron_type', 'email', 'account_status', 'registration_date')
    list_filter = ('patron_type', 'account_status', 'registration_date')
    search_fields = ('fullname', 'email', 'contact_number')
    date_hierarchy = 'registration_date'


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'transaction_type', 'book', 'patron', 'processed_by', 'transaction_date', 'due_date', 'return_date', 'overdue_flag')
    list_filter = ('transaction_type', 'transaction_date', 'overdue_flag')
    search_fields = ('book__title', 'patron__fullname')
    raw_id_fields = ('patron', 'book', 'processed_by')
    date_hierarchy = 'transaction_date'


@admin.register(PatronLog)
class PatronLogAdmin(admin.ModelAdmin):
    list_display = ('log_id', 'patron', 'school', 'purpose_of_visit', 'entry_time', 'exit_time')
    list_filter = ('entry_time', 'exit_time')
    search_fields = ('patron__fullname',)
    raw_id_fields = ('patron',)
    date_hierarchy = 'entry_time'
