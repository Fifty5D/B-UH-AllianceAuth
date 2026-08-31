"""Effective-dated exemption matching across every linked Auth character."""

from __future__ import annotations

import hashlib
import json

from allianceauth.authentication.models import CharacterOwnership

from .models import TaxExemption


class ExemptionIndex:
    """One audit-wide snapshot of exemptions and current Auth ownership links."""

    def __init__(self):
        self.exemptions = list(
            TaxExemption.objects.filter(active=True).order_by(
                "-effective_from", "-pk"
            )
        )
        self.user_by_character = dict(
            CharacterOwnership.objects.values_list(
                "character__character_id", "user_id"
            )
        )

    def state_hash(self) -> str:
        """Fingerprint exemption rules and live Auth character ownerships."""

        payload = {
            "exemptions": [
                {
                    "id": item.pk,
                    "user_id": item.auth_user_id,
                    "character_id": item.character_id,
                    "from": item.effective_from.isoformat(),
                    "until": (
                        item.effective_until.isoformat()
                        if item.effective_until
                        else None
                    ),
                    "reason": item.reason,
                }
                for item in self.exemptions
            ],
            "owners": sorted(self.user_by_character.items()),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def user_id_for(self, character_id: int | None, fallback_user_id=None):
        """Prefer the character's current Auth owner over a stale stored link."""

        if character_id:
            current = self.user_by_character.get(int(character_id))
            if current:
                return current
        return fallback_user_id

    def match(
        self,
        *,
        user_id: int | None,
        character_id: int | None,
        day,
    ) -> TaxExemption | None:
        """Return the most recent active exemption covering this account/character."""

        linked_user_id = self.user_id_for(character_id, user_id)
        for exemption in self.exemptions:
            if exemption.effective_from > day:
                continue
            if exemption.effective_until and exemption.effective_until < day:
                continue
            if character_id and exemption.character_id == character_id:
                return exemption
            if linked_user_id and exemption.auth_user_id == linked_user_id:
                return exemption
        return None
