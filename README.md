# LFTR Broadcast GFS Marine Globe

> **GCP Build Spec is included at the top of this full README.**  
> Use this section first when making a brand-new Google Cloud VM, then run either the `.run` installer or the zip installer from your home directory.

## GCP Build Spec — New Google Cloud VM

Recommended machine:

| Setting | Value |
|---|---|
| Cloud provider | Google Cloud Platform / Compute Engine |
| Machine family | General purpose |
| Machine type | `e2-standard-2` |
| CPU | 2 vCPU |
| Memory | 8 GB RAM |
| Architecture | x86/64 / amd64 |
| OS | Debian 13 “Trixie” x86/64 if available; Debian 12 “Bookworm” x86/64 fallback |
| Boot disk | 20 GB minimum SSD/balanced persistent disk; 30–50 GB recommended |
| Firewall | Check **Allow HTTP traffic** and **Allow HTTPS traffic** |
| External IP | Static external IPv4 strongly recommended |
| Cloud API access scopes | Select **Allow full access to all Cloud APIs** |
| Service account | Default Compute Engine service account is okay for first install; dedicated service account is better later |

Why this shape: `e2-standard-2` gives LFTR enough room for Quart/Hypercorn, nginx, WebRTC signaling, Certbot, GFS payload work, cache updates, and Python package setup. A smaller 1 vCPU server can boot the app, but it is more likely to stall or feel slow during install and weather/ocean cache work.

### GCP console checklist

1. Go to **Compute Engine → VM instances → Create instance**.
2. Name the VM something like `lftr-broadcast-gfs-globe`.
3. Pick a nearby region/zone, for example `us-west1`, `us-west2`, or `us-central1`.
4. Machine type: choose **E2 → e2-standard-2**.
5. Boot disk:
   - OS: **Debian GNU/Linux**
   - Version: **Debian 13 Trixie x86/64** if available
   - Fallback: **Debian 12 Bookworm x86/64**
   - Disk type: Balanced persistent disk or SSD persistent disk
   - Size: **20 GB minimum**, **30–50 GB recommended**
6. Firewall:
   - Check **Allow HTTP traffic**
   - Check **Allow HTTPS traffic**
7. Identity and API access:
   - Service account: default is okay for first install
   - Access scopes: select **Allow full access to all Cloud APIs**
8. Create the VM.
9. Reserve or assign a static external IPv4.
10. Point your domain DNS `A` record to that external IPv4.

### DNS requirement for certificates

Before running the installer, the domain must point to this VM.

On the VM, check:

```bash
curl -4 ifconfig.me; echo
sudo apt-get update && sudo apt-get install -y dnsutils
DOMAIN=lftr.biz
dig +short A "$DOMAIN"
dig +short AAAA "$DOMAIN"
```

The `A` record must match the VM external IPv4. If an `AAAA` record exists but the VM is not serving IPv6, remove the `AAAA` record temporarily or Let’s Encrypt may validate the wrong address.

### Ports needed

| Port | Purpose |
|---|---|
| 22/tcp | SSH admin access |
| 80/tcp | Let’s Encrypt HTTP challenge and HTTP redirect |
| 443/tcp | HTTPS app traffic |

The installer uses nginx webroot ACME validation, so port 80 must be reachable from the public internet during certificate issue/renewal.

---

## App overview

Quart + Hypercorn web app with `/broadcast`, `/watch`, and `/gfs`, including the neon terminal installer, GFS Marine Globe, port 80 Certbot webroot flow, and nightfall sky atmosphere work.

The final live app directory is always:

```bash
~/broadcast
```

---

## Easiest install: one-file self-extracting `.run`

If you have `LFTR-Broadcast-GFS-Marine-Globe.run`, copy/download it into your home directory and run:

```bash
cd ~
chmod +x LFTR-Broadcast-GFS-Marine-Globe.run
sudo ./LFTR-Broadcast-GFS-Marine-Globe.run
```

The `.run` installer:

- knows its stable project name: `LFTR-Broadcast-GFS-Marine-Globe`
- detects its own filename at runtime, so renamed builds still work
- installs bootstrap packages before extracting itself
- creates `~/broadcast`
- stages the app into `~/broadcast`
- starts `broadcast.sh`

---

## Zip install option

Upload or download `LFTR-Broadcast-GFS-Marine-Globe.zip` into your home directory, then run:

```bash
cd ~
sudo apt-get update && sudo apt-get install -y unzip
rm -rf LFTR-Broadcast-GFS-Marine-Globe broadcast
unzip LFTR-Broadcast-GFS-Marine-Globe.zip -d LFTR-Broadcast-GFS-Marine-Globe
cd LFTR-Broadcast-GFS-Marine-Globe
sudo bash INSTALL_NOW.sh
```

`INSTALL_NOW.sh` will create `~/broadcast`, copy the package into it, move into `~/broadcast`, and start the real installer.

---

## Installer highlights

The installer includes:

- neon/color terminal UI
- `whiptail` / `dialog` terminal boxes when available
- `figlet`, `toilet`, and `lolcat` banner support when available
- safe ANSI fallback if graphical terminal packages are unavailable
- package bootstrap for `unzip`, `curl`, `ca-certificates`, and supporting packages
- nginx webroot Certbot flow instead of fragile Certbot standalone mode
- temporary HTTP nginx ACME challenge server on port 80
- final HTTPS nginx config after cert issue
- systemd service setup for the app

---

## After install checks

```bash
sudo systemctl status broadcast --no-pager
sudo systemctl status nginx --no-pager
journalctl -u broadcast -n 120 --no-pager
curl -I http://lftr.biz
curl -I https://lftr.biz
```

---

## GCP APIs / keys you may need

The app normally needs a Google Maps JavaScript API key for the Photorealistic 3D globe. In Google Cloud, make sure the relevant Maps APIs are enabled for the project and that your key is allowed for your domain.

For server-side Google Cloud access from the VM, using **Allow full access to all Cloud APIs** prevents VM OAuth scope problems during early testing. Later, tighten IAM/service-account roles after the app is stable.

---

## Minimum fallback VM

If cost is the only priority, this can run on a custom 1 vCPU / 8 GB RAM VM, but expect slower installs and slower GFS/cache work. The recommended stable baseline is still:

```text
e2-standard-2, Debian x86/64, 20–50 GB disk, HTTP/HTTPS enabled, full API access
```
