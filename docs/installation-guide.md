# Installation guide

This setup provisions a BIND9 server directly with DNS data from NetBox. BIND9 can optionally run on
a separate server. If so, any reference to 127.0.0.1 in step 6 must be replaced with the IP address
of the NetBox host. TCP and UDP traffic from the BIND9 server to the NetBox host must be allowed on
port 5354 (or the port you have configured).

This guide assumes:
- NetBox has been installed under /opt/netbox
- BIND9 is installed on the same host as NetBox
- The NetBox DNS plugin netbox-plugin-dns is installed
- The following DNS views exist in NetBox DNS:
    - `public` (the default)
    - `private`

## 1. Preliminaries

- Install BIND9 on the same host that NetBox is on.
- Generate a TSIG key for the `public` and `private` DNS views respectively.

## 2. Adding required package

```
cd netbox
echo netbox-plugin-dns-bridge >> local_requirements.txt
. venv/bin/activate
pip install -r local_requirements.txt
```

## 3. Updating netbox plugin configuration (configuration.py)

Change the following line from

```
PLUGINS = ['netbox_dns']
```

to

```
PLUGINS = ['netbox_dns', 'netbox_dns_bridge']
```

Configure the DNS Bridge plugin using the `PLUGINS_CONFIG` dictionary. Change

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

Note that the tsig-key attributes keyname, algorithm and secret form a dictionary in the following
Python structure path:

```
PLUGINS_CONFIG.netbox_dns_bridge.tsig_keys.<dns_view_name>
```

This allows the plugin to map requests to the right DNS view using the TSIG signature from each
request.

## 4. Run migrations

```
manage.py migrate
```

## 5. Start listener

This step runs the DNS endpoint used by BIND to configure itself. You may want to write a service
wrapper that runs this in the background. A guide for setting up a systemd service on Ubuntu is
provided in [install-systemd-service.md](install-systemd-service.md). Don't forget to activate the
venv if you do decide to run this service in the background.

Note that `--port 5354` is optional. The listener will bind this port by default.

```
manage.py dns-transfer-endpoint --port 5354
```

## 6. Configuring BIND9

Configure BIND9 to interact with NetBox via the dns-transfer-endpoint endpoint. Note that it is
not possible to give all the correct details of the `options` block as it is heavily dependent on
the operating system used. Please don't forget to adjust it as required.

> **Note:** Since BIND 9.18, `type slave` is deprecated in favor of `type secondary` and
> `masters` in favor of `primaries`. Both still work, but newer deployments may prefer the new
> keywords.

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

########## VIEWS & ZONES ##########

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

## 7. Restart BIND9

Restart BIND9 — done. The server will now discover zones in its views through the catalog zone and
transfer them from NetBox.
