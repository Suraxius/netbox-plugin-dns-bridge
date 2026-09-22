import dns.message
import dns.name
import dns.opcode
import dns.query
import dns.rdatatype
import dns.rdataclass
import dns.rrset
import dns.tsig
from django.conf import settings
from django.utils import timezone
from django.db import close_old_connections, OperationalError
from netbox.jobs import JobRunner
from netbox_dns.models import View, Zone, Record
from netbox_dns_bridge.utils import get_logger
from netbox_dns_bridge.models import SeenTransferClient

LOGGER = get_logger(__name__)


class NotifyJobRunner(JobRunner):
    """Base class for NOTIFY jobs: shared plugin settings and helpers."""

    SETTINGS = settings.PLUGINS_CONFIG.get("netbox_dns_bridge", {})

    def load_tsig_key(self, view_name: str) -> dns.tsig.Key:
        tsig_keys = self.SETTINGS.get("tsig_keys", {})
        if view_name not in tsig_keys:
            raise RuntimeError(f"No TSIG key configured for view '{view_name}'")

        key_data = tsig_keys[view_name]
        raw_key_name = key_data.get("keyname")
        secret = key_data.get("secret")
        algorithm = key_data.get("algorithm")
        if not algorithm:
            raise RuntimeError(
                f"Missing 'algorithm' in TSIG key configuration for view '{view_name}'"
            )

        if not raw_key_name or not secret:
            raise RuntimeError(f"Incomplete TSIG key configuration for view '{view_name}'")

        key_name = dns.name.from_text(raw_key_name, origin=None).canonicalize()
        if not key_name.is_absolute():
            key_name = key_name.concatenate(dns.name.root)

        return dns.tsig.Key(name=key_name, secret=secret, algorithm=algorithm)

    def send_notify(
        self,
        zone_name,
        soa_serial,
        targets,
        tsig_key,
        view_name,
        notify_over_tcp,
        port,
        log_prefix="NOTIFY",
        soa_refresh=60,
        soa_retry=10,
        soa_expire=1209600,
        soa_minimum=0,
    ):
        """Build a DNS NOTIFY message for *zone_name* and send it to every target IP."""
        fqdn = dns.name.from_text(zone_name, dns.name.root).to_text()
        msg = dns.message.make_query(fqdn, dns.rdatatype.SOA)
        msg.flags |= dns.flags.AA
        msg.set_opcode(dns.opcode.NOTIFY)

        soa_rdata = dns.rdata.from_text(
            dns.rdataclass.IN,
            dns.rdatatype.SOA,
            f"invalid. invalid. {soa_serial} {soa_refresh} {soa_retry} {soa_expire} {soa_minimum}",
        )
        msg.answer.append(dns.rrset.from_rdata(fqdn, 0, soa_rdata))
        msg.use_tsig(keyring={tsig_key.name: tsig_key}, keyname=tsig_key.name)

        for target in targets:
            try:
                if notify_over_tcp:
                    LOGGER.debug(f"Sending {log_prefix} over TCP")
                    dns.query.tcp(msg, target, port=port, timeout=2)
                else:
                    LOGGER.debug(f"Sending {log_prefix} over UDP")
                    dns.query.udp(msg, target, port=port, timeout=2)

                LOGGER.info(
                    f"{log_prefix} sent: {target} {view_name}/{fqdn} {soa_serial}"
                )
            except Exception as e:
                LOGGER.error(
                    f"{log_prefix} failed: {target}:{port}"
                    f" {view_name}/{fqdn} {soa_serial}: {e}"
                )

    @staticmethod
    def get_soa_serial(zone: Zone):
        """Return the current SOA serial of a zone parsed from its SOA record."""
        try:
            soa_record = Record.objects.filter(zone=zone, type="SOA", active=True).first()
            if soa_record is None:
                LOGGER.error(f"Zone {zone.name} has no active SOA record — skipping NOTIFY")
                return None

            return int(soa_record.value.split()[2])

        except OperationalError as e:
            LOGGER.error(f"DB ERROR while fetching SOA for zone {zone.name}: {e}")
            close_old_connections()
            return None
        except (ValueError, IndexError) as e:
            LOGGER.error(f"Failed to parse SOA serial for zone {zone.name}: {e}")
            return None


class SendClientNotify(NotifyJobRunner):
    class Meta:
        name = "Send DNS NOTIFY"

    def run(self, *args, **kwargs):
        notify_over_tcp = self.SETTINGS.get("notify_over_tcp", False)
        port = self.SETTINGS.get("notify_client_port", 53)
        view_name = kwargs["view_name"]
        zone_name = kwargs["zone_name"]
        soa_serial = kwargs["soa_serial"]
        soa_refresh = kwargs.get("soa_refresh", 60)
        soa_retry = kwargs.get("soa_retry", 10)
        soa_expire = kwargs.get("soa_expire", 1209600)
        soa_minimum = kwargs.get("soa_minimum", 0)

        try:
            view = View.objects.get(name=view_name)
            tsig_key = self.load_tsig_key(view_name)
            cutoff_hours = self.SETTINGS.get("notify_client_alive_threshold_hours", 24)
            seen_cutoff = timezone.now() - timezone.timedelta(hours=cutoff_hours)

            clients = list(
                SeenTransferClient.objects.filter(
                    view=view, last_seen__gte=seen_cutoff
                ).values_list("source_ip", flat=True)
            )

            self.send_notify(
                zone_name, soa_serial, clients, tsig_key,
                view_name, notify_over_tcp, port, log_prefix="NOTIFY",
                soa_refresh=soa_refresh, soa_retry=soa_retry,
                soa_expire=soa_expire, soa_minimum=soa_minimum,
            )

        except View.DoesNotExist as e:
            LOGGER.error(f"Failed to get view for NOTIFY: {e}")

        except OperationalError as e:
            LOGGER.error(f"NOTIFY failed due to unexpected error: {e}")
            close_old_connections()


class SendNSNotify(NotifyJobRunner):
    """Send a NOTIFY straight to a zone's own nameservers.

    Unlike ``SendClientNotify`` (which notifies clients that previously queried
    the transfer endpoint), this job resolves the addresses of the zone's
    configured nameservers from NetBox DNS itself and notifies each of them so
    they pick up the changed zone quickly.
    """

    class Meta:
        name = "Send DNS NOTIFY to zone nameservers"

    def run(self, *args, **kwargs):
        notify_over_tcp = self.SETTINGS.get("notify_over_tcp", False)
        port = self.SETTINGS.get("notify_ns_port", 53)
        view_name = kwargs["view_name"]
        zone_name = kwargs["zone_name"]
        soa_serial = kwargs["soa_serial"]

        try:
            zone = Zone.objects.get(pk=kwargs["zone_id"])
        except Zone.DoesNotExist:
            LOGGER.error(f"NS NOTIFY: zone id {kwargs['zone_id']} no longer exists")
            return
        except OperationalError as e:
            LOGGER.error(f"NS NOTIFY failed due to unexpected error: {e}")
            close_old_connections()
            return

        ns_targets = self.resolve_zone_nameserver_ips(zone)
        if not ns_targets:
            LOGGER.warning(
                f"NS NOTIFY: no nameserver addresses found in NetBox DNS for "
                f"{view_name}/{zone_name} — skipping"
            )
            return

        try:
            tsig_key = self.load_tsig_key(view_name)
        except RuntimeError as e:
            LOGGER.error(f"NS NOTIFY aborted for {view_name}/{zone_name}: {e}")
            return

        self.send_notify(
            zone_name, soa_serial, ns_targets, tsig_key,
            view_name, notify_over_tcp, port, log_prefix="NS NOTIFY",
        )

    @classmethod
    def notify_ns_enabled(cls, zone) -> bool:
        """Return True if a NOTIFY to the zone's nameservers should be sent.

        Enabled either globally for every zone via ``notify_ns_all_zones`` or per
        zone when the boolean custom field named by ``notify_ns_custom_field_name``
        is set to True on the zone.
        """
        if cls.SETTINGS.get("notify_ns_all_zones", False):
            return True

        cf_name = cls.SETTINGS.get("notify_ns_custom_field_name")
        if not cf_name:
            return False

        custom_fields = getattr(zone, "custom_field_data", None) or {}
        return custom_fields.get(cf_name) is True

    def resolve_zone_nameserver_ips(self, zone: Zone) -> list:
        """Resolve the zone's nameserver hostnames to IPs using NetBox DNS records.

        For each nameserver assigned to the zone, matching active A/AAAA records
        within the same view are looked up in NetBox DNS. Returns a de-duplicated,
        order-preserving list of IP addresses.
        """
        ips = []
        try:
            nameservers = list(zone.nameservers.all())
            view = zone.view

            for nameserver in nameservers:
                fqdn = dns.name.from_text(nameserver.name, dns.name.root).to_text()
                # Match both with and without a trailing dot to be resilient against
                # how the fqdn is stored on the Record model.
                candidates = {fqdn, fqdn.rstrip(".")}

                records = Record.objects.filter(
                    fqdn__in=list(candidates),
                    type__in=["A", "AAAA"],
                    active=True,
                    zone__view=view,
                )
                for record in records:
                    if record.value not in ips:
                        ips.append(record.value)

        except OperationalError as e:
            LOGGER.error(f"DB ERROR while resolving nameservers for {zone.name}: {e}")
            close_old_connections()

        return ips


def schedule_client_notify(zone: Zone):
    if not SendClientNotify.SETTINGS.get("notify_clients", False):
        return

    soa_serial = SendClientNotify.get_soa_serial(zone)
    if soa_serial is None:
        return

    SendClientNotify.enqueue(
        zone_name=zone.name,
        view_name=zone.view.name,
        soa_serial=soa_serial,
    )


def schedule_ns_notify(zone: Zone):
    if not SendNSNotify.notify_ns_enabled(zone):
        return

    soa_serial = SendNSNotify.get_soa_serial(zone)
    if soa_serial is None:
        return

    SendNSNotify.enqueue(
        zone_id=zone.pk,
        zone_name=zone.name,
        view_name=zone.view.name,
        soa_serial=soa_serial,
    )
