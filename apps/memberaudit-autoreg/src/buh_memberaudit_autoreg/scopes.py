"""Scope configuration shared by Alliance Auth settings and this addon.

The Member Audit list matches ``Character.esi_scopes()`` in aa-memberaudit 5.0.4.
Keeping the list in a model-free module makes it safe to import from ``local.py``
while Django is still loading settings.
"""

MEMBERAUDIT_ESI_SCOPES = (
    "esi-assets.read_assets.v1",
    "esi-calendar.read_calendar_events.v1",
    "esi-characters.read_agents_research.v1",
    "esi-characters.read_blueprints.v1",
    "esi-characters.read_contacts.v1",
    "esi-characters.read_corporation_roles.v1",
    "esi-characters.read_fatigue.v1",
    "esi-characters.read_fw_stats.v1",
    "esi-characters.read_loyalty.v1",
    "esi-characters.read_medals.v1",
    "esi-characters.read_notifications.v1",
    "esi-characters.read_standings.v1",
    "esi-characters.read_titles.v1",
    "esi-clones.read_clones.v1",
    "esi-clones.read_implants.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-corporations.read_corporation_membership.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-industry.read_character_mining.v1",
    "esi-killmails.read_killmails.v1",
    "esi-location.read_location.v1",
    "esi-location.read_online.v1",
    "esi-location.read_ship_type.v1",
    "esi-mail.read_mail.v1",
    "esi-markets.read_character_orders.v1",
    "esi-markets.structure_markets.v1",
    "esi-planets.manage_planets.v1",
    "esi-planets.read_customs_offices.v1",
    "esi-search.search_structures.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-skills.read_skills.v1",
    "esi-universe.read_structures.v1",
    "esi-wallet.read_character_wallet.v1",
)

# Alliance Auth uses this setting for initial registration, EVE SSO login,
# changing a main character, and adding characters.
LOGIN_TOKEN_SCOPES = ["publicData", *MEMBERAUDIT_ESI_SCOPES]

