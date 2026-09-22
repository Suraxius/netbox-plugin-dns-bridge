# NetBox DNS Bridge

The NetBox DNS Bridge plugin extends [NetBox DNS](https://github.com/sys4/netbox-plugin-dns) by
embedding a lightweight DNS server directly within NetBox. It acts as a bridge between NetBox DNS
and your existing DNS infrastructure, leveraging native DNS mechanisms for seamless integration.
These include zone transfers (RFC 5936), catalog zones (RFC 9432), and notifying clients about zone
changes (RFC 1996).

<a href="https://pypi.org/project/netbox-plugin-dns-bridge/"><img src="https://img.shields.io/pypi/v/netbox-plugin-dns-bridge" alt="PyPi"></a>
<a href="https://github.com/suraxius/netbox-plugin-dns-bridge/stargazers"><img src="https://img.shields.io/github/stars/suraxius/netbox-plugin-dns-bridge?style=flat" alt="Stars Badge"></a>
<a href="https://github.com/suraxius/netbox-plugin-dns-bridge/network/members"><img src="https://img.shields.io/github/forks/suraxius/netbox-plugin-dns-bridge?style=flat" alt="Forks Badge"></a>
<a href="https://github.com/suraxius/netbox-plugin-dns-bridge/issues"><img src="https://img.shields.io/github/issues/suraxius/netbox-plugin-dns-bridge" alt="Issues Badge"></a>
<a href="https://github.com/suraxius/netbox-plugin-dns-bridge/pulls"><img src="https://img.shields.io/github/issues-pr/suraxius/netbox-plugin-dns-bridge" alt="Pull Requests Badge"></a>
<a href="https://github.com/suraxius/netbox-plugin-dns-bridge/graphs/contributors"><img src="https://img.shields.io/github/contributors/suraxius/netbox-plugin-dns-bridge?color=2b9348" alt="GitHub contributors"></a>
<a href="https://github.com/suraxius/netbox-plugin-dns-bridge/blob/master/LICENSE"><img src="https://img.shields.io/github/license/suraxius/netbox-plugin-dns-bridge?color=2b9348" alt="License Badge"></a>
<a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000.svg" alt="Code Style Black"></a>
<a href="https://pepy.tech/project/netbox-plugin-dns-bridge"><img src="https://static.pepy.tech/personalized-badge/netbox-plugin-dns-bridge?period=total&left_color=BLACK&right_color=BLUE&left_text=Downloads" alt="Downloads"></a>
<a href="https://pepy.tech/project/netbox-plugin-dns-bridge"><img src="https://static.pepy.tech/personalized-badge/netbox-plugin-dns-bridge?period=monthly&left_color=BLACK&right_color=BLUE&left_text=Downloads%2fMonth" alt="Downloads/Month"></a>
<a href="https://pepy.tech/project/netbox-plugin-dns-bridge"><img src="https://static.pepy.tech/personalized-badge/netbox-plugin-dns-bridge?period=weekly&left_color=BLACK&right_color=BLUE&left_text=Downloads%2fWeek" alt="Downloads/Week"></a>


# Plugin configuration
While providing Zone transfers via AXFR, the Server also exposes specialized catalog zones that BIND
and other RFC9432 compliant DNS Servers use to automatically discover newly created zones and remove
deleted ones. The plugin supports views and basic DNS security via TSIG.

The plugin exposes one catalog zone per view. The catalog zone name is configurable (see
[Catalog Zone Naming](#catalog-zone-naming) below) and defaults to **"catz"**. It may be queried
through the built-in DNS server just like any other dns zone.

For proper operation, each view requires an installed TSIG key, and the `dns-transfer-endpoint` must
be running as a separate background service using the `manage.py` command. Note that DNSSEC support
will be added once BIND9 provides a mechanism to configure it through the Catalog Zones system.

To start the transfer endpoint service in the foreground:
```
manage.py dns-transfer-endpoint --port 5354
```
This process needs to be scheduled as a background service for the built-in DNS Server to work
correctly. For Linux users with Systemd (Ubuntu, etc), Matt Kollross provides a startup unit and
instructions [here](docs/install-systemd-service.md).


## Service parameters
Parameter | Description
--------- | -------------------------------------------------------------------
--port    | Port to listen on for requests (defaults to 5354)
--address | IP of interface to bind to (defaults to 0.0.0.0)


## Plugin settings
Plugin settings should be configured under `PLUGINS_CONFIG` in `netbox_dns_bridge`:
```
PLUGINS_CONFIG = {
    'netbox_dns_bridge': {
        ...
        ...
    }
}
```

### Catalog Zone Naming
The name of the catalog zone exposed for each view is configurable:

Setting | Default | Description
------- | ------- | -----------------------------------------------------------
`catalog_zone_name` | `catz` | The name of the catalog zone. May be a full DNS name.
`catalog_zone_name_includes_view` | `False` | Prepend the view name: `<view>.<catalog_zone_name>`.

`catalog_zone_name` accepts full DNS names with multiple labels. Case and a trailing dot are
irrelevant — names are handled and compared as `dns.name.Name` objects. With
`catalog_zone_name_includes_view` disabled (the default), the plain `catalog_zone_name` is used for
every view; when enabled, the view name is prepended (e.g. `_default_.catz`).

```
'netbox_dns_bridge': {
    'catalog_zone_name': 'catz',
    'catalog_zone_name_includes_view': False,
    ...
}
```

Note that the DNS server consuming the catalog zone must be configured with the same zone name,
e.g. BIND9's `catalog-zones { zone "<catalog_zone_name>" ... }` statement and the corresponding
slave zone declaration must match.

### TSIG Authentication
Following sets the TSIG key that allows clients to query the transfer endpoint and also the key to
be used to sign NOTIFY messages to clients on zone changes. Each view should have its own unique
key to allow the plugin to identify the view the client is trying to access. Re-using the key for
multiple views yields unpredicted behavior.
```
'tsig_keys': {
    'the-view-name-here': {
        'keyname': 'the-tsig-key-name-here',
        'algorithm': 'hmac-sha256',
        'secret': 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    }
}
```

### NOTIFY
The plugin has 2 different NOTIFY mechanisms that may be turned on individually or together.

1. [Client Notify](#client-notify)
   Notify clients that are regularly contacting the transfer endpoint for zone updates.

2. [NS Notify](#ns-notify)
   Notify the zone's NS Servers when a zone changes.

   - [Per Zone](#ns-notify-per-zone)
     Notify NS servers for zones that have a specific custom field set to true.

   - [For all Zones](#ns-notify-for-all-zones)
     Notify NS servers for all zones.
     Note: This makes the *Per Zone* mechanism redundant. There is no point in enabling both.

#### Global NOTIFY Settings
```
'notify_over_tcp': True
```
Switch to using TCP instead of UDP. Defaults to False

#### Client NOTIFY
Clients may be notified of zone changes using the NOTIFY mechanism defined in RFC 1996.
When enabled, the zone transfer endpoint keeps track of any client that successfully queried the
endpoint. Once a zone has changed, NetBox informs each client about the zone changed so that the
client can request a new zone transfer.

Note that this feature uses NetBox's background job system to schedule the messages asynchronously.
In order to work, you need at least one `rq-worker` service running in the background to handle
the queued jobs.

```
'notify_clients': True
```
This enables the NOTIFY system. Defaults to False

```
'notify_client_alive_threshold_hours': 24
```
This sets how long a client is considred "alive" after it last queried the transfer endpoint. 
Once the client has failed to check in for this amout of time, it is automatically removed from the
list of clients to be notified on zone changes. Default is 24 hours.

```
'notify_client_port': 53
```
Destination port used to send NOTIFY messages to clients. Defaults to 53.



#### NS NOTIFY
In addition to notifying clients that queried the transfer endpoint, the plugin can send a NOTIFY
directly to a zone's own nameservers whenever that zone's SOA serial number changes. This is useful
when you want nameservers to pull updates immediately, independent of the `notify_clients` mechanism.
Only a change of the SOA serial triggers this — other zone edits that leave the serial untouched do
not.

This can be enabled per zone, globally, or both. If neither of the settings below is configured, the
feature is disabled entirely.


##### NS NOTIFY per Zone
This uses a True/False custom field attached to each Zone to enable/disable NOTIFY for each
individually.

Create a boolean custom field (e.g. `dns_bridge_notify_ns`) that applies
to `netbox_dns > zone`, then point the plugin at it:
```
'notify_ns_custom_field_name': 'dns_bridge_notify_ns'
```
A NOTIFY is then sent for any zone whose custom field is set to `True`. This lets you toggle the
behavior per zone straight from the NetBox UI, without changing the plugin config.


##### NS NOTIFY for all Zones
```
'notify_ns_all_zones': True
```
When enabled, a NOTIFY is sent for every zone on serial change, regardless of the custom field.
Defaults to `False`. The two options coexist: a NOTIFY is sent if the global switch is on **or** the
zone's custom field is set.


##### NS NOTIFY Settings

The nameserver addresses are resolved from NetBox DNS itself by looking up active A/AAAA records
(within the zone's view) matching each nameserver's hostname. The NOTIFY messages are signed with the
same per-view TSIG key configured under `tsig_keys`, so the receiving nameservers must accept NOTIFY
signed with that key (e.g. BIND's `allow-notify { key "..."; };`).

```
'notify_ns_port': 53
```
Destination port used for these NOTIFY messages. Defaults to 53.

As with the client NOTIFY feature, this uses NetBox's background job system, so at least one
`rq-worker` service must be running.


## Plugin compatibility
This plugin extends the netbox-plugin-dns plugin. As such the versioning was changed to match the
one of netbox-plugin-dns more closely. To guarantee compatability, ensure that the major and minor
version match between both plugins. For example, when using netbox-plugin-dns `v1.5.5` install
netbox-plugin-dns-bridge `v1.5.x`.


## Installation guide
This setup provisions a BIND9 server directly with DNS data from NetBox. BIND9 can optionally run on
a separate server. If so, any reference to 127.0.0.1 in step 6 must be replaced with the IP address
of the NetBox host. TCP and UDP traffic from the BIND9 server to the NetBox host must be allowed on
port 5354 (or the port you have configured).

This guide assumes:
- NetBox has been installed under /opt/netbox
- Bind9 is installed on the same host as NetBox
- The NetBox DNS Plugin netbox-plugin-dns is installed
- The following dns views exist in NetBox DNS:
    - `public` (the default)
    - `private`

1. Preliminaries
    - Install Bind9 on the same host that netbox is on.
    - Generate a TSIG Key for the `public` and `private` dns views respectively.

2. Adding required package
    ```
    cd netbox
    echo netbox-plugin-dns-bridge >> local_requirements.txt
    . venv/bin/activate
    pip install -r local_requirements.txt
    ```

3. Updating netbox plugin configuration (configuration.py)
    Change following line from
    ```
    PLUGINS = ['netbox_dns']
    ```
    to
    ```
    PLUGINS = ['netbox_dns', 'netbox_dns_bridge']
    ```

    Configure the DNS Bridge Plugin using the PLUGINS_CONFIG dictionary.
    Change
    ```
    PLUGINS_CONFIG = {}
    ```
    to
    ```
    PLUGINS_CONFIG = {
        "netbox_dns_bridge": {
            "tsig_keys": {
                "public": {
                    "keyname":   "public_view_key",
                    "algorithm": "hmac-sha256",
                    "secret":    "base64-encoded-secret"
                },
                "private": {
                    "keyname":   "private_view_key",
                    "algorithm": "hmac-sha256",
                    "secret":    "base64-encoded-secret"
                }
            }
        }
    }
    ```
    Note that the tsig-key attributes keyname, algorithm and secret form a
    dictionary in following python structure path:
    ```
    PLUGINS_CONFIG.netbox_dns_bridge.tsig_keys.<dns_view_name>
    ```
    This allows the plugin to map requests to the right dns view using the tsig
    signature from each request.

4. Run migrations
    ```
    manage.py migrate
    ```

5. Start listener

    This step runs the DNS endpoint used by bind to configure itself. You may want to write a
    service wrapper that runs this in the background. A guide for setting up a systemd service on
    Ubuntu is provided by Matt Kollross [here](docs/install-systemd-service.md). Dont forget to
    activate the venv if you do decide to run this service in the background.

    Note that `--port 5354` is optional. The listener will bind this port by default.
    ```
    manage.py dns-transfer-endpoint --port 5354
    ```

6. Configuring a Bind9 to interact with NetBox via the dns-transfer-endpoint endpoint. Note that its
    not possible to give all the correct details of the `options` block as it is heavily dependent
    on the Operating System used. Please dont forget to adjust as required.
   
    ```
    ########## OPTIONS ##########

    options {
        allow-update      { none; };
        allow-query       { any; };
        allow-recursion   { none; };
        notify            yes;
        min-refresh-time  60;
    };

    ########## ACLs ##########

    acl public {
        !10.0.0.0/8;
        !172.16.0.0/12;
        !192.168.0.0/16;
        any;
    };

    acl private {
        10.0.0.0/8;
        172.16.0.0/12;
        192.168.0.0/16;
    };

    ######## TSIG Keys ########
        key "public_view_key" {
            algorithm hmac-sha256;
            secret "base64-encoded-secret";
        };

        key "private_view_key" {
            algorithm hmac-sha256;
            secret "base64-encoded-secret";
        };
    ###########################


    ########## ZONES ##########
    view "public" {
        match-clients { public; };
        allow-notify { key "public_view_key"; };

        catalog-zones {
            zone "catz"
                default-masters { 127.0.0.1 port 5354 key "public_view_key"; }
                zone-directory "/var/lib/bind/zones"
                min-update-interval 1;
        };

        zone "catz" {
            type slave;
            file "/var/lib/bind/zones/catz_public";
            masters { 127.0.0.1 port 5354 key "public_view_key"; };
            notify no;
        };
    };

    view "private" {
        match-clients { private; };
        allow-notify { key "private_view_key"; };

        catalog-zones {
            zone "catz"
                default-masters { 127.0.0.1 port 5354 key "private_view_key"; }
                zone-directory "/var/lib/bind/zones"
                min-update-interval 1;
        };

        zone "catz" {
            type slave;
            file "/var/lib/bind/zones/catz_private";
            masters { 127.0.0.1 port 5354 key "private_view_key"; };
            notify no;
        };
    };
    ```

7. Restart bind - Done


