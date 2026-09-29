# NetBox DNS transfer endpoint systemd service

1. Create a systemd unit file
    ```
    cat <<EOF > /etc/systemd/system/netbox-dns-endpoint.service
    [Unit]
    Description=Netbox DNS transfer endpoint
    Documentation=https://github.com/Suraxius/netbox-plugin-dns-bridge
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple

    User=netbox
    Group=netbox
    PIDFile=/var/tmp/netbox.pid
    WorkingDirectory=/opt/netbox

    ExecStart=/opt/netbox/venv/bin/python3  /opt/netbox/netbox/manage.py dns-transfer-endpoint --address 0.0.0.0 --port 5354

    Restart=on-failure
    RestartSec=30
    PrivateTmp=true

    [Install]
    WantedBy=multi-user.target
    EOF
    ```

2. Enable and start the service
    ```
    systemctl enable netbox-dns-endpoint
    systemctl start netbox-dns-endpoint
    systemctl status netbox-dns-endpoint
    ```
