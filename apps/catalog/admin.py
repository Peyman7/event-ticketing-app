from django.contrib import admin

from .models import Event, Organizer, TicketType

admin.site.register(Organizer)
admin.site.register(Event)
admin.site.register(TicketType)
