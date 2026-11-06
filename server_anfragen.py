"""
Fragt bei Oracle Cloud so lange nach einem Gratis-Server (Ampere A1),
bis einer frei ist. Danach stoppt das Skript und meldet sich.

Voraussetzungen (siehe ANLEITUNG.md):
- Datei C:\\Users\\sapir\\.oci\\config mit dem Oracle-API-Schluessel
- Netzwerk "trading-bot-netz" existiert schon
- SSH-Schluessel (.pub) liegt in C:\\Users\\sapir\\.ssh\\oracle\\
"""
import glob
import os
import sys
import time
from datetime import datetime

try:
    import oci
except ImportError:
    print("Paket 'oci' fehlt. Bitte server_anfragen_starten.bat benutzen.")
    input("Enter zum Schliessen ...")
    sys.exit(1)

# ---- Einstellungen ----------------------------------------------------
SERVER_NAME = "trading-bot"
NETZ_NAME = "trading-bot-netz"
SSH_ORDNER = os.path.expanduser(r"~\.ssh\oracle")
GROESSEN = [(2, 12), (1, 6)]   # zuerst 2 Kerne/12 GB, sonst 1 Kern/6 GB
WARTEN_SEKUNDEN = 90            # Pause zwischen zwei Runden
SPEICHER_GB = 50
# -----------------------------------------------------------------------


def log(text):
    zeile = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}"
    print(zeile, flush=True)
    with open(os.path.join(os.path.dirname(__file__), "anfragen.log"), "a", encoding="utf-8") as f:
        f.write(zeile + "\n")


def meldung(titel, text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, titel, 0x40)
    except Exception:
        pass


def ssh_schluessel_laden():
    dateien = glob.glob(os.path.join(SSH_ORDNER, "*.pub"))
    if not dateien:
        log(f"Kein oeffentlicher SSH-Schluessel (.pub) in {SSH_ORDNER} gefunden.")
        sys.exit(1)
    neueste = max(dateien, key=os.path.getmtime)
    log(f"SSH-Schluessel: {os.path.basename(neueste)}")
    with open(neueste, encoding="utf-8") as f:
        return f.read().strip()


def main():
    config = oci.config.from_file()
    oci.config.validate_config(config)
    compartment = config["tenancy"]

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    netz = oci.core.VirtualNetworkClient(config)

    ad = identity.list_availability_domains(compartment).data[0].name
    log(f"Region: {config['region']}, Verfuegbarkeitsbereich: {ad}")

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
    log(f"Subnetz: {subnetze[0].display_name}")

    images = compute.list_images(
        compartment,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    images = [i for i in images if "Minimal" not in i.display_name]
    if not images:
        log("Kein passendes Ubuntu-24.04-Image gefunden.")
        sys.exit(1)
    image_id = images[0].id
    log(f"Image: {images[0].display_name}")

    ssh_key = ssh_schluessel_laden()
    runde = 0

    while True:
        # Sicherheit: nie einen zweiten Server anlegen
        vorhandene = [i for i in compute.list_instances(compartment).data
                      if i.display_name == SERVER_NAME
                      and i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
        if vorhandene:
            log(f"Server '{SERVER_NAME}' existiert bereits (Status {vorhandene[0].lifecycle_state}). Fertig.")
            meldung("Oracle-Server", "Dein Server 'trading-bot' existiert. Schau in der Oracle-Konsole nach.")
            return

        runde += 1
        for kerne, ram in GROESSEN:
            details = oci.core.models.LaunchInstanceDetails(
                availability_domain=ad,
                compartment_id=compartment,
                display_name=SERVER_NAME,
                shape="VM.Standard.A1.Flex",
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=kerne, memory_in_gbs=ram),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image_id, boot_volume_size_in_gbs=SPEICHER_GB),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnetz_id, assign_public_ip=True),
                metadata={"ssh_authorized_keys": ssh_key},
            )
            try:
                antwort = compute.launch_instance(details)
                log(f"ERFOLG mit {kerne} Kern(en) / {ram} GB! Server wird gestartet.")
                meldung("Oracle-Server gefunden!",
                        f"Dein Gratis-Server ({kerne} Kerne, {ram} GB) wird erstellt.\n"
                        "Schau in ein paar Minuten in der Oracle-Konsole nach.")
                return
            except oci.exceptions.ServiceError as e:
                text = (e.message or "").lower()
                if "capacity" in text:
                    log(f"Runde {runde}: kein Platz fuer {kerne}/{ram} GB.")
                elif e.status == 429:
                    log("Zu viele Anfragen, warte 5 Minuten.")
                    time.sleep(300)
                elif e.status in (500, 502, 503, 504):
                    log(f"Oracle-Fehler {e.status}, versuche weiter.")
                else:
                    log(f"Unerwarteter Fehler {e.status} {e.code}: {e.message}")
                    log("Skript stoppt. Schick diese Meldung an Claude.")
                    meldung("Oracle-Skript gestoppt", f"Fehler: {e.code}\n{e.message}")
                    return
            time.sleep(5)
        time.sleep(WARTEN_SEKUNDEN)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Von Hand beendet.")
    except Exception as e:
        log(f"Fehler: {e}")
    input("Enter zum Schliessen ...")
