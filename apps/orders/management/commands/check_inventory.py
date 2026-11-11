from django.core.management.base import BaseCommand, CommandError

from apps.orders.reconcile import inventory_mismatches


class Command(BaseCommand):
    help = "Verify reserved_count == held + sold for every ticket type."

    def handle(self, *args, **options):
        bad = inventory_mismatches()
        if bad:
            for row in bad:
                self.stderr.write(str(row))
            raise CommandError(f"{len(bad)} ticket type(s) out of balance")
        self.stdout.write(self.style.SUCCESS("inventory consistent"))
