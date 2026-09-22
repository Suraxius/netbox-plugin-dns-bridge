import socketserver
import socket
import dns.query
import dns.message
import dns.tsigkeyring
import dns.name
import dns.zone
import dns.rdatatype
import dns.rdataclass
import dns.rdtypes
import dns.exception
import dns.renderer
from django.utils import timezone
from django.db import close_old_connections, OperationalError
from django.conf import settings
from netbox_dns.models import Zone, View
from . import catalog_zone_manager as catzm
from netbox_dns_bridge.models import SeenTransferClient
from netbox_dns_bridge.utils import build_dns_zone, get_logger

LOGGER = get_logger(__name__)
SETTINGS = settings.PLUGINS_CONFIG.get("netbox_dns_bridge", {})


class DNSBaseRequestHandler(socketserver.BaseRequestHandler):
    transport = None

    def __init__(self, request, client_address, server) -> None:
        self.MAX_WIRE = 65535
        self.RESERVED_TSIG = 300
        super().__init__(request, client_address, server)

    # _get_zone_from_nb rewritten
    def _get_zone_from_nb(self, zone_name, view_name) -> dns.zone.Zone:
        # Find the zone
        try:
            nb_zone = Zone.objects.get(
                name=zone_name,
                view__name=view_name,
                active=True,
            )
            return build_dns_zone(nb_zone)
        except Zone.DoesNotExist:
            return None
        except OperationalError as e:
            LOGGER.error(f"DB ERROR: {e}")
            close_old_connections()

    def _deny_request_bad_tsig(self, wire, tsig_error: dns.rcode) -> None:
        # Use empty keyring to parse TSIG without validating
        query = dns.message.from_wire(
            wire, keyring={}, ignore_trailing=True, continue_on_error=True
        )

        # Make a response
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.REFUSED)

        if query.had_tsig:
            # Add TSIG with error code, but do not sign (no MAC, empty keyring)
            response.use_tsig(
                keyring={},  # empty; we're not signing
                keyname=query.keyname,
                tsig_error=tsig_error,
            )
        self._send_response(response.to_wire())

    def _send_response(self, data) -> None:
        raise NotImplementedError

    def _deny_request(self, query, rcode: dns.rcode = dns.rcode.REFUSED) -> None:
        response = dns.message.make_response(query)
        response.set_rcode(rcode)
        wire = response.to_wire(multi=False)
        self._send_response(wire)

    def _track_seen_client(self, source_ip: str, view: View) -> None:
        if SETTINGS.get("notify_clients", False):
            try:
                SeenTransferClient.objects.update_or_create(
                    source_ip=source_ip,
                    view=view,
                    defaults={"last_seen": timezone.now()},
                )

            except OperationalError as e:
                LOGGER.error(f"DB ERROR: {e}")
                close_old_connections()

    def _handle_soa_request(self, query, soa_rrset, zone, peer, nb_view, dname) -> None:
        # We assume that the SOA rdataset has at least one record (it usually does).
        soa_rdata = soa_rrset[0]  # Get the first SOA record

        # Now, create the rrset from the soa_rdata
        rrset = dns.rrset.from_rdata(zone.origin, soa_rrset.ttl, soa_rdata)

        response = dns.message.make_response(query)
        # Set the Authoritative Answer flag
        response.flags |= dns.flags.AA

        # Append the rrset to the response's answer section
        response.answer.append(rrset)

        # TSIG response
        if query.had_tsig:
            # If key was found in DB
            if query.keyname in self.server.keyring:
                response.use_tsig(
                    self.server.keyring, keyname=query.keyname, original_id=query.id
                )
            else:
                # unknown key — use an empty keyring so use_tsig() does not
                # raise KeyError trying to look up a key that isn't present.
                response.set_rcode(dns.rcode.REFUSED)
                response.use_tsig(
                    {},  # empty keyring; no signing
                    keyname=query.keyname,
                    tsig_error=dns.rcode.BADKEY,
                )

        data = response.to_wire(max_size=512)
        self._send_response(data)
        LOGGER.info(f"{peer} SOA {nb_view.name}/{dname}")

    def _handle_axfr_request(self, query, zone, peer, nb_view, dname) -> None:
        if query.keyname not in self.server.keyring:
            LOGGER.error(f"AXFR aborted: keyname {query.keyname} not in keyring")
            self._deny_request(query)
            return

        tsig_key = self.server.keyring[query.keyname]

        rrsets = []
        soa_rrset = None
        for name, rdataset in zone.iterate_rdatasets():
            if not name.is_absolute():
                name = name.concatenate(zone.origin)
            rrset = dns.rrset.from_rdata_list(name, rdataset.ttl, rdataset)
            if rdataset.rdtype == dns.rdatatype.SOA and soa_rrset is None:
                soa_rrset = rrset
            else:
                rrsets.append(rrset)

        if soa_rrset is None:
            LOGGER.error(f"Zone {dname} has no SOA — aborting")
            return

        rrsets.insert(0, soa_rrset)  # Opening SOA
        rrsets.append(soa_rrset)  # Closing SOA

        # 2. Create a Renderer for the first message
        r = dns.renderer.Renderer(
            id=query.id,
            flags=(dns.flags.QR | dns.flags.AA),
            max_size=self.MAX_WIRE,
            origin=None,
        )
        r.add_question(
            query.question[0].name,
            query.question[0].rdtype,
            query.question[0].rdclass,
        )

        # 3. Loop through RRsets
        tsig_ctx = None
        for rrset in rrsets:
            try:
                r.add_rrset(dns.renderer.ANSWER, rrset)
                if r.max_size - len(r.output.getvalue()) < self.RESERVED_TSIG:
                    raise dns.exception.TooBig("TSIG wont fit")
            except dns.exception.TooBig:
                # TSIG chain previous message
                r.write_header()
                tsig_ctx = r.add_multi_tsig(
                    ctx=tsig_ctx,
                    secret=tsig_key.secret,
                    algorithm=tsig_key.algorithm,
                    keyname=query.keyname,
                    fudge=300,
                    id=query.id,
                    tsig_error=0,
                    other_data=b"",
                    request_mac=r.mac if tsig_ctx else query.mac,
                )
                wire = r.get_wire()
                self.request.sendall(len(wire).to_bytes(2, "big") + wire)

                # Start new renderer
                r = dns.renderer.Renderer(
                    id=query.id,
                    flags=(dns.flags.QR | dns.flags.AA),
                    max_size=self.MAX_WIRE,
                    origin=None,
                )
                r.add_question(
                    query.question[0].name,
                    query.question[0].rdtype,
                    query.question[0].rdclass,
                )

                try:
                    r.add_rrset(dns.renderer.ANSWER, rrset)
                except dns.exception.TooBig:
                    LOGGER.error(
                        f"RRset {rrset.name}/{dns.rdatatype.to_text(rrset.rdtype)} "
                        f"exceeds MAX_WIRE ({self.MAX_WIRE}) and cannot be sent; "
                        f"skipping."
                    )

        # 4. Final message with terminating TSIG
        r.write_header()
        tsig_ctx = r.add_multi_tsig(
            ctx=tsig_ctx,
            secret=tsig_key.secret,
            algorithm=tsig_key.algorithm,
            keyname=query.keyname,
            fudge=300,
            id=query.id,
            tsig_error=0,
            other_data=b"",
            request_mac=r.mac if tsig_ctx else query.mac,
        )
        wire = r.get_wire()
        self._send_response(wire)
        LOGGER.info(f"{peer} AXFR {nb_view.name}/{dname}")

    def _handle_dns_query(self, wire) -> None:
        peer = self.client_address[0]
        try:
            query = dns.message.from_wire(
                wire,
                keyring=self.server.keyring,
                continue_on_error=False,
                ignore_trailing=True,
            )

        except dns.tsig.BadSignature as e:
            LOGGER.warning(f"Request denied from {peer} failed TSIG verification: {e}")
            self._deny_request_bad_tsig(wire, dns.rcode.BADSIG)
            return

        except (dns.message.UnknownTSIGKey, dns.tsig.BadAlgorithm) as e:
            LOGGER.warning(f"Request denied from {peer} with bad TSIG key: {e}")
            self._deny_request_bad_tsig(wire, dns.rcode.BADKEY)
            return

        except Exception as e:
            LOGGER.error(f"Error parsing query: {e}")
            return

        # If there was no question in the query, refuse
        if len(query.question) != 1:
            self._deny_request(query)
            return

        question = query.question[0]
        qname = question.name
        qtype = question.rdtype
        dname = qname.to_text().rstrip(".")

        # Only process AXFR/SOA queris
        if qtype not in (dns.rdatatype.AXFR, dns.rdatatype.SOA):
            LOGGER.warning(
                f"Request denied from {peer}: Request was not AXFR or SOA (Type: {qtype})"
            )
            self._deny_request(query)
            return

        if qtype == dns.rdatatype.AXFR and self.transport != "tcp":
            LOGGER.warning(f"Request denied from {peer}: AXFR requires TCP")
            self._deny_request(query, dns.rcode.REFUSED)
            return

        # Identify TSIG key used
        if not query.had_tsig:
            LOGGER.warning(f"Request denied from {peer}: No TSIG key used")
            self._deny_request(query)
            return

        key_name = query.keyname.canonicalize().to_text()

        # Check if the key matches a view
        nb_view = self.server.tsig_view_map.get(key_name)
        if not nb_view:
            LOGGER.warning(
                f"Request denied from {peer}: {key_name} does not match a view"
            )
            self._deny_request(query)
            return

        # Check if catalog zone
        if catzm.is_catalog_zone_query(qname, nb_view.name):
            zone = catzm.create_zone(nb_view.name)
        else:
            zone = self._get_zone_from_nb(dname, nb_view.name)
        # When zone was not found, let client know
        if not zone:
            LOGGER.warning(f"Zone {dname} not found in view {nb_view.name}")
            self._deny_request(query)
            return

        # Retrieve the existing SOA record from the Zone
        soa_rrset = zone.get_rdataset(zone.origin, dns.rdatatype.SOA)
        if soa_rrset is None:
            LOGGER.error(f"Zone {dname} has no SOA — aborting")
            return

        if qtype == dns.rdatatype.SOA:
            self._handle_soa_request(query, soa_rrset, zone, peer, nb_view, dname)
            self._track_seen_client(peer, nb_view)
        elif qtype == dns.rdatatype.AXFR:
            self._handle_axfr_request(query, zone, peer, nb_view, dname)
            self._track_seen_client(peer, nb_view)


class UDPRequestHandler(DNSBaseRequestHandler):
    transport = "udp"

    def __init__(self, request, client_address, server) -> None:
        super().__init__(request, client_address, server)

    def _send_response(self, data) -> None:
        sock = self.request[1]
        sock.sendto(data, self.client_address)

    def handle(self) -> None:
        data, sock = self.request
        peer = self.client_address[0]
        try:
            self._handle_dns_query(data)
        except Exception as e:
            LOGGER.error(f"Error handling request from {peer}: {e}")
            import traceback

            traceback.print_exc()


class TCPRequestHandler(DNSBaseRequestHandler):
    transport = "tcp"

    def __init__(self, request, client_address, server) -> None:
        super().__init__(request, client_address, server)

    def _send_response(self, data) -> None:
        length = len(data).to_bytes(2, byteorder="big")
        self.request.sendall(length + data)

    def handle(self) -> None:
        peer = self.client_address[0]
        sock = self.request  # TCP socket
        sock.settimeout(10.0)  # Default 10 second timeout for inactivity
        try:
            while True:
                # Read 2-byte length prefix
                length_data = sock.recv(2)
                if not length_data:
                    return  # connection closed by peer
                if len(length_data) < 2:
                    # Incomplete length data, wait for more or fail
                    while len(length_data) < 2:
                        chunk = sock.recv(2 - len(length_data))
                        if not chunk:
                            return
                        length_data += chunk

                length = int.from_bytes(length_data, byteorder="big")
                wire = b""
                while len(wire) < length:
                    chunk = sock.recv(length - len(wire))
                    if not chunk:
                        return  # connection closed
                    wire += chunk
                self._handle_dns_query(wire)

        except socket.timeout:
            LOGGER.debug(f"Connection from {peer} timed out")
        except Exception as e:
            LOGGER.error(f"Error handling request from {peer}: {e}")
            import traceback

            traceback.print_exc()
