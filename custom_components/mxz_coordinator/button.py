"""Per-zone buttons (the way back from a per-room drift override)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one follow-global drift button, and two follow-derived band
    edge buttons, per zone."""
    coordinator: MXZCoordinator = entry.runtime_data
    entities: list[ButtonEntity] = [
        MXZDriftFollowGlobalButton(coordinator, zone) for zone in coordinator.zones
    ]
    for zone in coordinator.zones:
        entities.append(MXZBandEdgeFollowDerivedButton(coordinator, zone, edge="low"))
        entities.append(MXZBandEdgeFollowDerivedButton(coordinator, zone, edge="high"))
    async_add_entities(entities)


class MXZDriftFollowGlobalButton(MXZEntity, ButtonEntity):
    """Hand this room's drift band back to the global one (#18 follow-up).

    A room follows the global drift until someone writes its drift number;
    that write is an override and nothing released it. Retyping today's global
    value does not: an override that happens to equal the global is still an
    override, and it stops tracking the next options change. Pressing this
    drops the override, so the room is a follower again — the same state it
    was in before anyone touched it.

    One room, one setting. It leaves the target, the engage latch, the room
    enable, the fan hold and every lockout exactly where they were, and it
    touches no other room. CONFIG category, like the drift number it belongs
    to: on the device page, out of auto-populated dashboards and voice.
    """

    _attr_icon = "mdi:backup-restore"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_drift_follow_global")
        self._zone = zone
        self._drift_unique_id = (
            f"{coordinator.config_entry.entry_id}_{zone.slug}_drift"
        )
        self._last_drift_available: bool | None = None
        self._attr_translation_key = "zone_drift_follow_global"
        self._attr_translation_placeholders = {"zone": zone.name}

    async def async_added_to_hass(self) -> None:
        """Track the persistence owner through registry and state changes."""
        await super().async_added_to_hass()
        self._last_drift_available = self._drift_available()
        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_owner_update
            )
        )

        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED,
                self._handle_owner_update,
                event_filter=self._is_owner_state,
            )
        )

    @callback
    def _is_owner_state(self, data: dict) -> bool:
        """Track the current owner ID, including registry renames."""
        return data["entity_id"] == er.async_get(self.hass).async_get_entity_id(
            "number", DOMAIN, self._drift_unique_id
        )

    def _drift_available(self) -> bool:
        """Return whether the room's drift number can persist this action."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "number", DOMAIN, self._drift_unique_id
        )
        if entity_id is None:
            return False
        entry = registry.async_get(entity_id)
        if entry is None or entry.disabled:
            return False
        # Registry re-enable precedes HA's delayed reload. Only a live owner
        # with a published state can save the override changed by this press.
        component = self.hass.data.get("entity_components", {}).get("number")
        owner = component.get_entity(entity_id) if component is not None else None
        state = self.hass.states.get(entity_id)
        return (
            owner is not None
            and owner.hass is self.hass
            and owner.coordinator is self.coordinator
            and state is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        )

    @callback
    def _handle_owner_update(self, _event: Event) -> None:
        """Redraw only when the drift number's availability changed."""
        available = self._drift_available()
        if available == self._last_drift_available:
            return
        self._last_drift_available = available
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Expose the action only while its restore owner is ready."""
        return self._drift_available()

    async def async_press(self) -> None:
        """Drop this room's override and recompute on the global band.

        Following is the ABSENCE of an override, so clearing it is the whole
        change — nothing copies the global value anywhere. The listeners run
        before the (debounced) refresh so the room's drift number redraws the
        global, and its ``override`` attribute, at once; that written state is
        also what restore reads back after a restart.

        The engage latch is deliberately NOT reset, exactly as when the drift
        number is written: a band change redraws the coast window, it does not
        invalidate a run already headed for the (unchanged) target.
        """
        if not self._drift_available():
            raise HomeAssistantError(
                "Cannot follow global drift while this room's drift number is "
                "disabled or missing, or has not finished loading."
            )
        self._zone.drift = None
        self.coordinator.async_update_listeners()
        await self.coordinator.async_user_changed()


class MXZBandEdgeFollowDerivedButton(MXZEntity, ButtonEntity):
    """Drop this room's explicit band edge, going back to target +/- drift.

    Same pattern as ``MXZDriftFollowGlobalButton``: the edge is an override
    until cleared here. Leaves the other edge and everything else untouched.
    """

    _attr_icon = "mdi:backup-restore"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MXZCoordinator, zone: Zone, *, edge: str) -> None:
        assert edge in ("low", "high")
        super().__init__(coordinator, f"{zone.slug}_band_{edge}_follow_derived")
        self._zone = zone
        self._edge = edge
        self._band_unique_id = (
            f"{coordinator.config_entry.entry_id}_{zone.slug}_band_{edge}"
        )
        self._last_band_available: bool | None = None
        self._attr_translation_key = f"zone_band_{edge}_follow_derived"
        self._attr_translation_placeholders = {"zone": zone.name}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._last_band_available = self._band_available()
        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_owner_update
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED,
                self._handle_owner_update,
                event_filter=self._is_owner_state,
            )
        )

    @callback
    def _is_owner_state(self, data: dict) -> bool:
        return data["entity_id"] == er.async_get(self.hass).async_get_entity_id(
            "number", DOMAIN, self._band_unique_id
        )

    def _band_available(self) -> bool:
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "number", DOMAIN, self._band_unique_id
        )
        if entity_id is None:
            return False
        entry = registry.async_get(entity_id)
        if entry is None or entry.disabled:
            return False
        component = self.hass.data.get("entity_components", {}).get("number")
        owner = component.get_entity(entity_id) if component is not None else None
        state = self.hass.states.get(entity_id)
        return (
            owner is not None
            and owner.hass is self.hass
            and owner.coordinator is self.coordinator
            and state is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        )

    @callback
    def _handle_owner_update(self, _event: Event) -> None:
        available = self._band_available()
        if available == self._last_band_available:
            return
        self._last_band_available = available
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._band_available()

    async def async_press(self) -> None:
        if not self._band_available():
            raise HomeAssistantError(
                f"Cannot follow the derived band while this room's band_{self._edge} "
                "number is disabled or missing, or has not finished loading."
            )
        setattr(self._zone, f"band_{self._edge}", None)
        self.coordinator.async_update_listeners()
        await self.coordinator.async_user_changed()
