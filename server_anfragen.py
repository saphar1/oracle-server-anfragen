"""
GitHub-Version: fragt bei Oracle nach einem Gratis-Server (Ampere A1),
bis einer frei ist. Zugangsdaten kommen aus den GitHub-Secrets.
"""
import os
import sys
import time
from datetime import datetime, timezone

import oci

SERVER_NAME = "trading-bot"
NETZ_NAME = "trading-bot-netz"
GROESSEN = [(2, 12), (1, 6)]
WARTEN_SEKUNDEN = 75
SPEICHER_GB = 50
LAUFZEIT_SEKUNDEN = 5 * 3600 + 40 * 60   # knapp unter 6 Stunden (GitHub-Grenze)


def log(text):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {text}", flush=True)


def ergebnis(wert):
    pfad = os.environ.get("GITHUB_OUTPUT")
    if pfad:
        with open(pfad, "a") as f:
            f.write(f"ergebnis={wert}\n")


def main():
    config = {
        "user": os.environ["OCI_USER"].strip(),
        "fingerprint": os.environ["OCI_FINGERPRINT"].strip(),
        "tenancy": os.environ["OCI_TENANCY"].strip(),
        "region": os.environ["OCI_REGION"].strip(),
        "key_content": os.environ["OCI_KEY"].strip() + "\n",
    }
    oci.config.validate_config(config)
    compartment = config["tenancy"]
    ssh_key = os.environ["SSH_PUBLIC_KEY"].strip()

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    netz = oci.core.VirtualNetworkClient(config)

    ad = identity.list_availability_domains(compartment).data[0].name
    vcns = [v for v in netz.list_vcns(compartment).data
            if v.display_name == NETZ_NAME and v.lifecycle_state == "AVAILABLE"]
    if not vcns:
        log(f"Netzwerk '{NETZ_NAME}' nicht gefunden.")
        sys.exit(1)
    subnetze = [s for s in netz.list_subnets(compartment, vcn_id=vcns[0].id).data
                if not s.prohibit_public_ip_on_vnic and s.lifecycle_state == "AVAILABLE"]
    if not subnetze:
        log("Kein oeffentliches Subnetz gefunden.")
        sys.exit(1)
    subnetz_id = subnetze[0].id

    images = compute.list_images(
        compartment, operating_system="Canonical Ubuntu",
        operating_system_version="24.04", shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED", sort_order="DESC").data
    images = [i for i in images if "Minimal" not in i.display_name]
    if not images:
        log("Kein passendes Ubuntu-Image gefunden.")
        sys.exit(1)
    image_id = images[0].id
    log(f"Bereit. Subnetz: {subnetze[0].display_name}, Image: {images[0].display_name}")

    start = time.time()
    runde = 0
    while time.time() - start < LAUFZEIT_SEKUNDEN:
        vorhandene = [i for i in compute.list_instances(compartment).data
                      if i.display_name == SERVER_NAME
                      and i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
        if vorhandene:
            log(f"Server existiert bereits (Status {vorhandene[0].lifecycle_state}). Nichts zu tun.")
            ergebnis("vorhanden")
            return

        runde += 1
        # pro Runde nur eine Anfrage, Groessen abwechseln (Oracle bremst sonst mit 429)
        for kerne, ram in [GROESSEN[(runde - 1) % len(GROESSEN)]]:
            details = oci.core.models.LaunchInstanceDetails(
                availability_domain=ad, compartment_id=compartment,
                display_name=SERVER_NAME, shape="VM.Standard.A1.Flex",
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=kerne, memory_in_gbs=ram),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image_id, boot_volume_size_in_gbs=SPEICHER_GB),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnetz_id, assign_public_ip=True),
                metadata={"ssh_authorized_keys": ssh_key},
            )
            try:
                compute.launch_instance(details)
                log(f"ERFOLG mit {kerne} Kern(en) / {ram} GB!")
                ergebnis(f"erfolg-{kerne}-{ram}")
                return
            except oci.exceptions.ServiceError as e:
                text = (e.message or "").lower()
                if "capacity" in text:
                    log(f"Runde {runde}: kein Platz fuer {kerne}/{ram} GB.")
                elif e.status == 429:
                    log("Zu viele Anfragen, warte 3 Minuten.")
                    time.sleep(180)
                elif e.status in (500, 502, 503, 504):
                    log(f"Oracle-Fehler {e.status}, versuche weiter.")
                else:
                    log(f"Unerwarteter Fehler {e.status} {e.code}: {e.message}")
                    sys.exit(1)
        time.sleep(WARTEN_SEKUNDEN)
    log("Zeit abgelaufen, der naechste geplante Lauf macht weiter.")


if __name__ == "__main__":
    main()
