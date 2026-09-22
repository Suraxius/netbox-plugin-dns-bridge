import dns.name
import dns.zone
import dns.rdatatype
import dns.rdataclass
from netbox_dns_bridge.utils import get_logger
from netbox_dns.models import Zone as NBZone, View as NBView
from netbox_dns_bridge.models import CatalogZone, CatalogZoneMember
from netbox_dns_bridge.jobs import notify
from uuid import uuid4
from base64 import b32encode
from django.conf import settings
from django.db import transaction, close_old_connections, OperationalError


LOGGER = get_logger(__name__)

SETTINGS = settings.PLUGINS_CONFIG.get("netbox_dns_bridge", {})


def catalog_zone_name(view_name: str) -> dns.name.Name:
    """Return the fully qualified catalog zone name for a view as a
    dns.name.Name.

    The name is configured via the ``catalog_zone_name`` setting (fallback:
    ``catz``). With ``catalog_zone_name_includes_view`` enabled, the view
    name is prepended: ``<view>.<catalog_zone_name>``.
    """
    base = dns.name.from_text(SETTINGS.get("catalog_zone_name") or "catz")
    if SETTINGS.get("catalog_zone_name_includes_view", False):
        return dns.name.from_text(view_name, origin=base)
    return base


def is_catalog_zone_query(qname: dns.name.Name, view_name: str) -> bool:
    """Check whether a queried name is the catalog zone name of a view.

    dns.name.Name equality is case-insensitive, so queries from BIND match
    regardless of case.
    """
    return qname == catalog_zone_name(view_name)


def increment_soa_serial(view: NBView) -> int:
    with transaction.atomic():
        try:
            (
                soa_serial_obj,
                _,
            ) = CatalogZone.objects.select_for_update().get_or_create(
                view=view,
                defaults={"soa_serial": 1},
            )

            new_soa_serial = soa_serial_obj.soa_serial + 1
            if new_soa_serial > 0xFFFFFFFF:
                LOGGER.warning(
                    f"Catalog serial {soa_serial_obj.soa_serial} reached max — wrapping back to 1"
                )
                new_soa_serial = 1
            soa_serial_obj.soa_serial = new_soa_serial
            soa_serial_obj.save()
            soa_serial_obj.refresh_from_db()
            LOGGER.debug(
                f"Catalog zone SOA serial for view '{view.name}' "
                f"is now {soa_serial_obj.soa_serial}"
            )

            serial = soa_serial_obj.soa_serial
        except OperationalError as e:
            LOGGER.error(
                f"ERROR: Failed to increment the Catalog Zone's SOA serial: {e}"
            )
            close_old_connections()
            return None

    schedule_catalog_zone_notify(view)
    return serial


def create_zone(view_name) -> dns.zone.Zone:
    # Zone origin: the configured catalog zone name for this view
    origin = catalog_zone_name(view_name)

    # Create a new empty zone
    zone = dns.zone.Zone(origin)
    zone.rdclass = dns.rdataclass.IN

    try:
        # get zones from netbox
        nb_zones = NBZone.objects.filter(
            view__name=view_name, active=True
        ).select_related("catalog_zone_member")

        ptr_base = dns.name.from_text("zones", origin)

        for nb_zone in nb_zones:
            ttl = 0
            qname = dns.name.from_text(nb_zone.name, dns.name.root)

            # Create PTR record
            try:
                member = nb_zone.catalog_zone_member
            except NBZone.catalog_zone_member.RelatedObjectDoesNotExist:
                catalog_zone = _get_or_create_catalog_zone(nb_zone.view)
                member = CatalogZoneMember.objects.create(
                    zone=nb_zone,
                    name=_generate_member_name(),
                    catalog_zone=catalog_zone,
                )

            p_name = member.name

            ptr_name = dns.name.from_text(p_name, ptr_base)
            if not ptr_name.is_subdomain(origin):
                raise ValueError(
                    f"Catalog zone member {ptr_name.to_text()} not a subdomain"
                )
            rdata = dns.rdata.from_text(
                dns.rdataclass.IN, dns.rdatatype.PTR, qname.to_text()
            )
            rdataset = zone.find_rdataset(ptr_name, dns.rdatatype.PTR, create=True)
            rdataset.add(rdata, ttl)

            # Configure DNSSec Policy for member Zone if DNSSec is enabled
            if nb_zone.dnssec_policy:
                rid = dns.name.from_text("group", ptr_name)
                policy_name = nb_zone.dnssec_policy.name.rstrip(" ")
                group_name = f"dnssec-policy-{policy_name}"
                rdata = dns.rdata.from_text(
                    dns.rdataclass.IN, dns.rdatatype.TXT, group_name
                )
                rdataset = zone.find_rdataset(rid, dns.rdatatype.TXT, create=True)
                rdataset.add(rdata, ttl)

        zone_root_node = zone.find_node(origin, create=True)

        # SOA record
        view = NBView.objects.get(name=view_name)
        soa_rdataset = _create_soa_rdataset(view)
        zone_root_node.rdatasets.append(soa_rdataset)

        # NS record
        ns_rdataset = _create_ns_rdataset()
        zone_root_node.rdatasets.append(ns_rdataset)

        # version.<catalog zone name>. record
        version_rdataset = _create_version_rdataset()
        zone_version_node = zone.find_node(
            dns.name.from_text("version", origin), create=True
        )
        zone_version_node.rdatasets.append(version_rdataset)

        return zone

    except OperationalError as e:
        LOGGER.error(f"ERROR: Failed to create Catalog Zone: {e}")
        close_old_connections()
        return None


def _generate_member_name() -> str:
    return b32encode(uuid4().bytes)[0:26].lower().decode("UTF-8")


def _get_or_create_catalog_zone(view: NBView) -> CatalogZone:
    catalog_zone, _ = CatalogZone.objects.get_or_create(
        view=view, defaults={"soa_serial": 1}
    )
    return catalog_zone


def update_member(zone: NBZone) -> None:
    try:
        catalog_zone = _get_or_create_catalog_zone(zone.view)
        CatalogZoneMember.objects.update_or_create(
            zone=zone,
            defaults={
                "name": _generate_member_name(),
                "catalog_zone": catalog_zone,
            },
        )
    except OperationalError as e:
        LOGGER.error(f"ERROR: Failed to update Catalog Zone member: {e}")
        close_old_connections()


def schedule_catalog_zone_notify(view: NBView) -> None:
    """Schedule a NOTIFY for the catalog zone of a view.

    Notifies transfer clients for the single configured catalog zone name
    (see ``catalog_zone_name``). The job receives the name as text, since
    job kwargs are serialized to JSON.
    """
    if not SETTINGS.get("notify_clients", False):
        return

    try:
        catalog_zone = view.catalog_zone
    except CatalogZone.DoesNotExist:
        return

    notify.SendClientNotify.enqueue(
        zone_name=catalog_zone_name(view.name).to_text(omit_final_dot=True),
        soa_serial=catalog_zone.soa_serial,
        view_name=view.name,
        soa_refresh=catalog_zone.soa_refresh,
        soa_retry=catalog_zone.soa_retry,
        soa_expire=catalog_zone.soa_expire,
        soa_minimum=catalog_zone.soa_minimum,
    )


def _create_soa_rdataset(view: NBView) -> dns.rdataset:
    try:
        serial_obj, _ = CatalogZone.objects.get_or_create(
            view=view, defaults={"soa_serial": 1}
        )
        serial = serial_obj.soa_serial

        # SOA Record components
        ttl = 0
        rclass = dns.rdataclass.IN
        rtype = dns.rdatatype.SOA

        # Create SOA rdata object
        rdata = dns.rdata.from_text(
            rclass,
            rtype,
            f"invalid. invalid. {serial} {serial_obj.soa_refresh} "
            f"{serial_obj.soa_retry} {serial_obj.soa_expire} "
            f"{serial_obj.soa_minimum}",
        )

        # Create Rdataset and add the RDATA to it
        rdataset = dns.rdataset.Rdataset(rclass, rtype)
        rdataset.add(rdata, ttl)
        return rdataset

    except OperationalError as e:
        LOGGER.error(f"ERROR: Failed to access catz SOA record in database: {e}")
        close_old_connections()


def _create_ns_rdataset() -> dns.rdataset:
    # NS record for catz.
    name = dns.name.from_text("invalid", dns.name.root)
    rdata = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.NS, str(name))
    rdataset = dns.rdataset.Rdataset(dns.rdataclass.IN, dns.rdatatype.NS)
    rdataset.add(rdata, 0)
    return rdataset


def _create_version_rdataset() -> dns.rdataset:
    rdata = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.TXT, '"2"')
    rdataset = dns.rdataset.Rdataset(dns.rdataclass.IN, dns.rdatatype.TXT)
    rdataset.add(rdata, 0)
    return rdataset
