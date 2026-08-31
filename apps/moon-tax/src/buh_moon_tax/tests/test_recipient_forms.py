from django.test import TestCase

from buh_moon_tax.forms import StructureTaxPolicyForm
from buh_moon_tax.models import PaymentRecipient


class RecipientChecklistTests(TestCase):
    def test_searchable_checklist_contains_characters_and_corporations(self):
        PaymentRecipient.objects.create(
            kind=PaymentRecipient.Kind.CHARACTER,
            entity_id=90000001,
            name="Fifty5D",
        )
        PaymentRecipient.objects.create(
            kind=PaymentRecipient.Kind.CORPORATION,
            entity_id=98000001,
            name="Bureau of Unified Harvesting",
        )
        html = str(StructureTaxPolicyForm()["payment_recipients"])
        self.assertIn("data-buh-recipient-picker", html)
        self.assertIn("Fifty5D", html)
        self.assertIn("Bureau of Unified Harvesting", html)
        self.assertIn("Select visible", html)
