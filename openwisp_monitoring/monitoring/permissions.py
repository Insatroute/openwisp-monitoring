"""
Group-wise device-visibility scoping for monitoring API views.

Mirrors ``controller_reports.permissions.get_visible_device_pks`` so that
live monitoring endpoints (WAN Uplinks, Data Usage, Mobile Distribution,
IPSec Tunnels, etc.) obey the same precedence rule as report exports:

  1. Superuser            -> None  (no filter, all devices visible)
  2. Has DeviceGroupUser  -> restricted to devices in those groups
                             (organization is ignored once group is set)
  3. Otherwise            -> devices in the user's organizations (legacy)
  4. Anonymous            -> empty queryset

Keeping this rule in ONE place means future endpoints can scope correctly
just by calling ``get_visible_device_ids(user)`` or wrapping a
``DeviceData`` queryset with ``scope_devicedata_qs(user, qs)``.
"""

from typing import Iterable, Optional

from swapper import load_model


def get_visible_device_ids(user) -> Optional[Iterable]:
    """
    Returns either:
      - ``None``                     -> no filter (superuser)
      - QuerySet of Device PKs       -> apply with ``.filter(pk__in=...)``
                                        or ``.filter(device_id__in=...)``.
    """
    Device = load_model("config", "Device")

    if not user or not getattr(user, "is_authenticated", False):
        return Device.objects.none().values_list("pk", flat=True)

    if user.is_superuser:
        return None

    # Device-group precedence: any DeviceGroupUser row -> confine to those
    # groups only. We deliberately ignore organization membership in this
    # branch so a group assignment is a hard whitelist, not an additional
    # filter on top of org membership.
    try:
        DeviceGroupUser = load_model("config", "DeviceGroupUser")
        group_ids = DeviceGroupUser.objects.filter(user=user).values_list(
            "device_group_id", flat=True,
        )
        if group_ids.exists():
            return Device.objects.filter(
                group_id__in=group_ids,
            ).values_list("pk", flat=True)
    except Exception:
        # Older deployments without DeviceGroupUser - fall through to org
        # scope below so we never silently expose all devices.
        pass

    # Legacy fallback: organization membership.
    from openwisp_users.models import OrganizationUser

    org_ids = OrganizationUser.objects.filter(user=user).values_list(
        "organization_id", flat=True,
    )
    return Device.objects.filter(
        organization_id__in=org_ids,
    ).values_list("pk", flat=True)


def scope_devicedata_qs(user, qs):
    """
    Apply group-wise visibility to a ``DeviceData`` queryset.

    ``DeviceData.pk`` is a OneToOne with ``Device.pk``, so filtering by
    ``pk__in=<visible Device pks>`` is correct and avoids a join.
    """
    visible = get_visible_device_ids(user)
    if visible is None:                  # superuser
        return qs
    return qs.filter(pk__in=visible)
