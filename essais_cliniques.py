"""
Veille essais cliniques en cancérologie v3
- Essais de phase 1 et 2 en recrutement
- Source : ClinicalTrials.gov API v2 (gratuite, officielle)
- Résumés en français via Groq
- Envoi hebdomadaire (vendredi matin)
"""

import requests
import smtplib
import json
import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
EMAIL_DEST     = os.environ["EMAIL_DEST"]
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
GITHUB_REPO    = os.environ["GITHUB_REPO"]

MAX_ESSAIS = 5
MEMORY_FILE = "sent_trials.json"
# ─────────────────────────────────────────────────────────────────────────────


# ── Mémoire GitHub ────────────────────────────────────────────────────────────

def load_memory():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_FILE}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 404:
        return {"sent": []}
    r.raise_for_status()
    import base64
    content = base64.b64decode(r.json()["content"]).decode("utf-8")
    data = json.loads(content)
    data["_sha"] = r.json()["sha"]
    return data


def save_memory(memory):
    import base64
    sha = memory.pop("_sha", None)
    content = base64.b64encode(json.dumps(memory, indent=2).encode()).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_FILE}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "message": f"Mise à jour mémoire essais {datetime.now().strftime('%Y-%m-%d')}",
        "content": content,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()


# ── ClinicalTrials.gov ────────────────────────────────────────────────────────

def fetch_trials():
    """Récupère les essais de phase 1-2 en oncologie."""
    url = "https://clinicaltrials.gov/api/v2/studies"

    # Deux requêtes séparées pour éviter les problèmes d'encodage avec les virgules
    all_studies = []
    for phase in ["PHASE1", "PHASE2"]:
        full_url = (
            f"{url}?query.cond=cancer+oncology+tumor"
            f"&filter.phase={phase}"
            f"&filter.overallStatus=RECRUITING"
            f"&pageSize=25"
            f"&format=json"
        )
        r = requests.get(full_url, timeout=30)
        r.raise_for_status()
        all_studies.extend(r.json().get("studies", []))

    # Déduplication par NCT ID
    seen = set()
    studies = []
    for s in all_studies:
        nct = s.get("protocolSection", {}).get("identificationModule", {}).get("nctId", "")
        if nct and nct not in seen:
            seen.add(nct)
            studies.append(s)

    trials = []
    for study in studies:
        try:
            proto = study.get("protocolSection", {})
            id_mod = proto.get("identificationModule", {})
            status_mod = proto.get("statusModule", {})
            desc_mod = proto.get("descriptionModule", {})
            design_mod = proto.get("designModule", {})
            arms_mod = proto.get("armsInterventionsModule", {})
            sponsor_mod = proto.get("sponsorCollaboratorsModule", {})
            locations_mod = proto.get("contactsLocationsModule", {})

            nct_id = id_mod.get("nctId", "")
            title = id_mod.get("briefTitle", "")
            summary = desc_mod.get("briefSummary", "")
            phases = design_mod.get("phases", [])
            phase_str = ", ".join(phases) if phases else "N/A"
            status = status_mod.get("overallStatus", "")
            sponsor = sponsor_mod.get("leadSponsor", {}).get("name", "")
            enrollment = design_mod.get("enrollmentInfo", {}).get("count", "N/A")

            interventions = arms_mod.get("interventions", [])
            intervention_names = [i.get("name", "") for i in interventions[:3]]
            intervention_str = ", ".join(filter(None, intervention_names)) or "N/A"

            locations = locations_mod.get("locations", [])
            countries = list(set(l.get("country", "") for l in locations if l.get("country")))
            countries_str = ", ".join(countries[:4]) if countries else "N/A"

            if not title or not summary or len(summary) < 50:
                continue

            trials.append({
                "nct_id": nct_id,
                "title": title,
                "summary": summary[:1500],
                "phase": phase_str,
                "status": status,
                "sponsor": sponsor,
                "intervention": intervention_str,
                "countries": countries_str,
                "enrollment": enrollment,
                "url": f"https://clinicaltrials.gov/study/{nct_id}",
            })
        except Exception:
            continue

    return trials


def pick_new_trials(trials, sent_ids):
    return [t for t in trials if t["nct_id"] not in sent_ids][:MAX_ESSAIS]


# ── Groq ──────────────────────────────────────────────────────────────────────

def summarize_trial(trial):
    prompt = f"""Tu es un oncologue expert. Résume cet essai clinique en français pour un étudiant en master d'oncologie.

Titre : {trial['title']}
Phase : {trial['phase']}
Traitement : {trial['intervention']}
Résumé : {trial['summary']}

Format exact attendu :
🎯 OBJECTIF : (1 phrase)
💊 TRAITEMENT TESTÉ : (nom et type)
👥 POPULATION CIBLE : (type de cancer et critères)
📊 DESIGN : (phase et nombre de patients)
🌍 CONTEXTE : (sponsor et pays)
💡 INTÉRÊT SCIENTIFIQUE : (1-2 phrases sur l'importance)"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 450,
        "temperature": 0.3,
    }
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_html(trials_with_summaries):
    date_str = datetime.now().strftime("%d %B %Y")
    count = len(trials_with_summaries)

    cards = ""
    for trial, summary in trials_with_summaries:
        summary_html = summary.replace("\n", "<br>")
        phase_color = "#EEF2FF" if "1" in trial['phase'] else "#F0FDF4"
        phase_text = "#4338CA" if "1" in trial['phase'] else "#166534"

        cards += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;
                    padding:24px;margin-bottom:20px;">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
            <span style="background:{phase_color};color:{phase_text};font-size:12px;
                         font-weight:600;padding:3px 10px;border-radius:20px;">{trial['phase']}</span>
            <span style="background:#FEF3C7;color:#92400E;font-size:12px;
                         font-weight:600;padding:3px 10px;border-radius:20px;">{trial['status']}</span>
            <span style="color:#9ca3af;font-size:12px;">{trial['nct_id']}</span>
          </div>
          <h3 style="margin:0 0 14px;font-size:15px;color:#111827;line-height:1.5;">
            {trial['title']}
          </h3>
          <div style="font-size:13px;color:#6b7280;margin-bottom:12px;">
            🏢 {trial['sponsor']} &nbsp;·&nbsp; 👥 {trial['enrollment']} patients &nbsp;·&nbsp; 🌍 {trial['countries']}
          </div>
          <div style="font-size:14px;color:#374151;line-height:1.8;">
            {summary_html}
          </div>
          <a href="{trial['url']}"
             style="display:inline-block;margin-top:14px;font-size:13px;
                    color:#4F46E5;text-decoration:none;">
            → Voir l'essai complet sur ClinicalTrials.gov
          </a>
        </div>"""

    if not trials_with_summaries:
        cards = """<div style="text-align:center;padding:40px;color:#6b7280;">
            Aucun nouvel essai trouvé cette semaine.
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">
    <div style="text-align:center;margin-bottom:28px;">
      <h1 style="font-size:22px;color:#111827;margin:0 0 6px;">🧪 Essais Cliniques Oncologie</h1>
      <p style="color:#6b7280;font-size:14px;margin:0;">
        Semaine du {date_str} · {count} essai(s) phase 1-2 · ClinicalTrials.gov
      </p>
    </div>
    {cards}
    <div style="text-align:center;margin-top:28px;color:#9ca3af;font-size:12px;">
      Généré automatiquement · Résumés par IA · Toujours vérifier la source originale
    </div>
  </div>
</body></html>"""


def send_email(html_content, count):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🧪 Essais Cliniques Oncologie — {count} essai(s) — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_DEST
    msg.attach(MIMEText(html_content, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, EMAIL_DEST, msg.as_string())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Démarrage veille essais cliniques v3...")

    print("  → Chargement de la mémoire...")
    memory = load_memory()
    sent_ids = set(memory.get("sent", []))

    print("  → Recherche essais sur ClinicalTrials.gov...")
    trials = fetch_trials()
    print(f"  → {len(trials)} essai(s) trouvé(s)")

    new_trials = pick_new_trials(trials, sent_ids)
    print(f"  → {len(new_trials)} nouvel(s) essai(s) sélectionné(s)")

    results = []
    for i, trial in enumerate(new_trials, 1):
        print(f"  → Résumé {i}/{len(new_trials)} : {trial['title'][:60]}...")
        summary = summarize_trial(trial)
        results.append((trial, summary))

    print("  → Envoi de l'email...")
    html = build_email_html(results)
    send_email(html, len(results))

    updated = list(sent_ids) + [t["nct_id"] for t in new_trials]
    memory["sent"] = updated[-1000:]
    save_memory(memory)

    print("  ✓ Terminé avec succès !")


if __name__ == "__main__":
    main()
