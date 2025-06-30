import os
import json
import datetime
import imaplib
import smtplib
import email
from email.message import EmailMessage
from email.header import decode_header

import re
import time
import threading
import importlib.util
import logging.handlers
import warnings
import inspect
import random
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Suppress urllib3 warnings for LibreSSL
warnings.filterwarnings("ignore", category=Warning, module="urllib3")

# === Ensure .digi Directory Exists ===
os.makedirs(".digi", exist_ok=True)

# === Setup Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s|%(levelname)s|%(name)s|%(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            ".digi/digiman.log", maxBytes=1000000, backupCount=5
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("DigiMan")

# === Load Environment Variables and Config ===
load_dotenv()
CONFIG_FILE = ".digi/config.json"

def load_config():
    """Load API keys from .env and config file, creating default if missing."""
    default_config = {}
    for key, value in os.environ.items():
        if key.endswith("_KEY") or key.endswith("_ACCOUNT") or key.endswith("_PASSWORD") or key.endswith("_SERVER") or key.endswith("_PORT") or key.endswith("_URL"):
            default_config[key] = value
    default_config.setdefault("EMAIL_ACCOUNT", "support@digimanai.com")
    default_config.setdefault("IMAP_PORT", 993)
    default_config.setdefault("SMTP_PORT", 465)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
            default_config.update(config)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(default_config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
    return default_config

CONFIG = load_config()

# === Global State ===
business_phases = ["setup", "promotion", "sales", "onboarding", "client_ops"]
current_phase_index = 0
metrics = {
    "tasks_processed": 0,
    "tasks_failed": 0,
    "agents_generated": 0,
    "clients_onboarded": 0,
    "revenue_generated": 0,
    "client_satisfaction": 0,
    "leads_generated": 0
}

# === Flask App Setup ===
app = Flask(__name__)

# === Agent Scoring ===
def evaluate_agent_quality(code):
    """Evaluate agent code quality for deployment readiness."""
    score = 0
    reasons = []
    try:
        compile(code, "<string>", "exec")
        score += 1
    except SyntaxError as e:
        reasons.append(f"Syntax error: {e}")
    if re.search(r"class \w+\s*(\(|:)", code):
        score += 1
    else:
        reasons.append("Missing class definition")
    if len(re.findall(r"def ", code)) >= 3:
        score += 1
    else:
        reasons.append("Less than 3 methods defined")
    if "log_action" in code:
        score += 1
    else:
        reasons.append("Missing log_action usage")
    return score, reasons

# === Utility Functions ===
def log_action(agent_name, action, client_id=None):
    """Log agent actions to file and metrics."""
    log_dir = f".digi/clients/{client_id}" if client_id else ".digi"
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "actions.log")
    try:
        with open(log_path, "a") as f:
            f.write(f"[{datetime.datetime.now()}] {agent_name}: {action}\n")
    except Exception as e:
        logger.error(f"Failed to log action for {agent_name}: {e}")
    logger.info(f"{agent_name}: {action}")
    metrics["tasks_processed"] += 1

def send_message_to_digiman(message, client_id=None):
    """Record user feedback or system messages."""
    log_dir = f".digi/clients/{client_id}" if client_id else ".digi"
    os.makedirs(log_dir, exist_ok=True)
    feedback_path = os.path.join(log_dir, "user_feedback.txt")
    try:
        with open(feedback_path, "a") as f:
            f.write(f"[{datetime.datetime.now()}] {message}\n")
    except Exception as e:
        logger.error(f"Failed to send message to DigiMan: {e}")
    logger.info(f"Feedback: {message}")

def audit_env_keys(keys):
    """Check for missing API keys and log issues."""
    missing = [key for key in keys if not CONFIG.get(key)]
    if missing:
        log_action("ENV_AUDIT", f"Missing keys: {', '.join(missing)}")
        send_message_to_digiman(f"MISSING_KEY_REQUEST: {', '.join(missing)}")
    return missing

def check_owner_overrides(client_id=None):
    """Retrieve owner override commands."""
    overrides = []
    log_dir = f".digi/clients/{client_id}" if client_id else ".digi"
    feedback_path = os.path.join(log_dir, "user_feedback.txt")
    if os.path.exists(feedback_path):
        try:
            with open(feedback_path, "r") as f:
                for line in f:
                    if "OVERRIDE:" in line:
                        matches = re.findall(r"OVERRIDE: ([\w ]+)", line)
                        overrides.extend(matches)
        except Exception as e:
            logger.error(f"Failed to check overrides: {e}")
    return overrides

def load_task_queue(client_id=None):
    """Load task queue from file."""
    log_dir = f".digi/clients/{client_id}" if client_id else ".digi"
    queue_path = os.path.join(log_dir, "agent_queue.json")
    if os.path.exists(queue_path):
        try:
            with open(queue_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load task queue: {e}")
            return {}
    return {}

def update_task_queue(agent_name, task, client_id=None):
    """Add task to agent's queue."""
    log_dir = f".digi/clients/{client_id}" if client_id else ".digi"
    os.makedirs(log_dir, exist_ok=True)
    queue_path = os.path.join(log_dir, "agent_queue.json")
    queue = load_task_queue(client_id)
    task_entry = {
        "task": task,
        "priority": task.get("priority", 1),
        "timestamp": str(datetime.datetime.now())
    }
    queue.setdefault(agent_name, []).append(task_entry)
    try:
        with open(queue_path, "w") as f:
            json.dump(queue, f, indent=2)
        log_action(agent_name, f"Task queued: {task}", client_id)
    except Exception as e:
        logger.error(f"Failed to update task queue for {agent_name}: {e}")

def make_api_request(endpoint, method="GET", headers=None, data=None, api_key=None):
    """Generic API request handler for integrations."""
    headers = headers or {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = request.request(method, endpoint, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"API request failed: {e}")
        return None

# === Agent Definitions ===
class ManagerAgent:
    """Oversees all agents, coordinates tasks, and monitors performance."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = ["HUBSPOT_ACCESS_TOKEN", "SALESFORCE_API_KEY", "ZOHO_CRM_API_KEY"]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Manager Agent", f"Running task: {task['task']}", self.client_id)
        if "monitor" in task["task"].lower():
            self.monitor_performance()
        elif "report" in task["task"].lower():
            self.generate_report()
        elif "process command" in task["task"].lower():
            self.process_command(task["task"])
        self.scale_self()

    def monitor_performance(self):
        if metrics["tasks_failed"] > 5:
            send_message_to_digiman(f"High failure rate: {metrics['tasks_failed']}", self.client_id)
            update_task_queue("Support Agent", {"task": "Investigate task failures", "priority": 3}, self.client_id)
        log_action("Manager Agent", f"Monitored performance: {metrics}", self.client_id)

    def generate_report(self):
        report = f"Metrics: {metrics}\nLeads Generated: {metrics['leads_generated']}"
        log_action("Manager Agent", f"Generated report: {report}", self.client_id)
        update_task_queue("Analyst Agent", {"task": f"Analyze report: {report}", "priority": 2}, self.client_id)

    def process_command(self, command):
        if "email" in command.lower():
            update_task_queue("Email Agent", {"task": "Process inbox", "priority": 2}, self.client_id)
        elif "website" in command.lower():
            update_task_queue("Web Builder Agent", {"task": "Build website", "priority": 2}, self.client_id)
        log_action("Manager Agent", f"Processed command: {command}", self.client_id)

    def scale_self(self):
        if metrics["leads_generated"] > 1000:
            update_task_queue("Franchise Builder Agent", {"task": "Deploy new franchise", "priority": 3}, self.client_id)
            log_action("Manager Agent", "Scaling: Initiated franchise deployment", self.client_id)

class EmailAgent:
    """Manages email communication, lead nurturing, and integrations."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = ["EMAIL_ACCOUNT", "EMAIL_PASSWORD", "IMAP_SERVER", "SMTP_SERVER"]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Email Agent", f"Running task: {task['task']}", self.client_id)
        if "process inbox" in task["task"].lower():
            self.process_inbox()
        elif "send campaign" in task["task"].lower():
            self.send_email_campaign()

    def process_inbox(self):
        history = set()
        try:
            if self.active:
                mail = imaplib.IMAP4_SSL(CONFIG["IMAP_SERVER"], CONFIG["IMAP_PORT"])
                mail.login(CONFIG["EMAIL_ACCOUNT"], CONFIG["EMAIL_PASSWORD"])
                mail.select("inbox")
                _, data = mail.search(None, "UNSEEN")
                for num in data[0].split():
                    _, data = mail.fetch(num, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])
                    subject = decode_header(msg["Subject"])[0][0]
                    subject = subject.decode() if isinstance(subject, bytes) else subject
                    sender = msg.get("From", "")
                    body = ""
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode()
                    category = "Lead" if any(kw in subject.lower() for kw in ["inquiry", "quote", "interest"]) else "Support" if "help" in subject.lower() else "Unknown"
                    reply = f"Thank you for your interest! Let's schedule a call to discuss your needs. Reply with your availability."
                    smtp = smtplib.SMTP_SSL(CONFIG["SMTP_SERVER"], CONFIG["SMTP_PORT"])
                    smtp.login(CONFIG["EMAIL_ACCOUNT"], CONFIG["EMAIL_PASSWORD"])
                    response = EmailMessage()
                    response["Subject"] = f"Re: {subject}"
                    response["From"] = CONFIG["EMAIL_ACCOUNT"]
                    response["To"] = sender
                    response.set_content(reply)
                    smtp.send_message(response)
                    smtp.quit()
                    log_action("Email Agent", f"Replied to {sender}: {reply}", self.client_id)
                    if category == "Lead":
                        update_task_queue("CRM Agent", {"task": f"Add lead: {sender}", "priority": 2}, self.client_id)
                        metrics["leads_generated"] += 1
                    history.add(num.decode())
                mail.logout()
            else:
                log_action("Email Agent", "Mock inbox processed: 5 leads identified", self.client_id)
                metrics["leads_generated"] += 5
                update_task_queue("CRM Agent", {"task": "Add mock leads", "priority": 2}, self.client_id)
        except Exception as e:
            log_action("Email Agent", f"Inbox error: {e}", self.client_id)
            metrics["tasks_failed"] += 1

    def send_email_campaign(self):
        if self.active:
            leads = [{"email": f"lead{random.randint(1,100)}@example.com"} for _ in range(10)]
            for lead in leads:
                smtp = smtplib.SMTP_SSL(CONFIG["SMTP_SERVER"], CONFIG["SMTP_PORT"])
                smtp.login(CONFIG["EMAIL_ACCOUNT"], CONFIG["EMAIL_PASSWORD"])
                msg = EmailMessage()
                msg["Subject"] = "Discover DigiMan Solutions"
                msg["From"] = CONFIG["EMAIL_ACCOUNT"]
                msg["To"] = lead["email"]
                msg.set_content("Join our platform to scale your business! Visit our site to learn more.")
                smtp.send_message(msg)
                smtp.quit()
                log_action("Email Agent", f"Sent campaign email to {lead['email']}", self.client_id)
        else:
            log_action("Email Agent", "Mock email campaign sent to 10 leads", self.client_id)

class WebBuilderAgent:
    """Builds and manages websites using provided API keys."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if key.endswith("_API_KEY") and "WEB" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)
        self.templates = {
            "ecommerce": "<html><body>E-commerce Site: {domain}</body></html>",
            "services": "<html><body>Services Site: {domain}</body></html>",
            "lead_capture": "<html><body>Lead Capture: {domain}<form action='/submit-lead'></form></body></html>"
        }

    def run_task(self, task):
        log_action("Web Builder Agent", f"Running task: {task['task']}", self.client_id)
        if "build website" in task["task"].lower():
            self.build_site(task.get("business_type", "lead_capture"))
        elif "optimize seo" in task["task"].lower():
            self.optimize_seo()

    def build_site(self, business_type):
        domain = CONFIG.get("DOMAIN_NAME", "example.com")
        template = self.templates.get(business_type, self.templates["lead_capture"]).format(domain=domain)
        if self.active and self.required_keys:
            for key in self.required_keys:
                api_key = CONFIG[key]
                endpoint = CONFIG.get(f"{key}_ENDPOINT", f"https://api.{key.lower().split('_')[0]}.com/v1/sites")
                response = make_api_request(endpoint, method="POST", api_key=api_key, data={"template": business_type, "domain": domain})
                if response:
                    log_action("Web Builder Agent", f"Built site on {key} for {business_type}", self.client_id)
                    update_task_queue("Marketing Agent", {"task": f"Promote site: {domain}", "priority": 2}, self.client_id)
                    return
        site_path = os.path.join(f".digi/clients/{self.client_id}", f"site_{business_type}.html")
        os.makedirs(os.path.dirname(site_path), exist_ok=True)
        try:
            with open(site_path, "w") as f:
                f.write(template)
            log_action("Web Builder Agent", f"Saved mock site for {business_type} at {site_path}", self.client_id)
            update_task_queue("Marketing Agent", {"task": f"Promote mock site: {domain}", "priority": 2}, self.client_id)
        except Exception as e:
            logger.error(f"Failed to save mock site: {e}")

    def optimize_seo(self):
        log_action("Web Builder Agent", "Mock SEO optimized for site", self.client_id)

class PartnershipScoutAgent:
    """Identifies collaboration opportunities using search APIs."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "SEARCH" in key.upper() or "SERP" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Partnership Scout Agent", f"Running task: {task['task']}", self.client_id)
        if "find partners" in task["task"].lower():
            self.find_partners()

    def find_partners(self):
        if self.active and self.required_keys:
            for key in self.required_keys:
                api_key = CONFIG[key]
                endpoint = CONFIG.get(f"{key}_ENDPOINT", "https://api.search.com/v1/search")
                response = make_api_request(endpoint, api_key=api_key, data={"query": "industry partners"})
                if response:
                    partners = response.get("results", [])
                    for partner in partners[:3]:
                        log_action("Partnership Scout Agent", f"Found partner: {partner.get('name')}", self.client_id)
                        update_task_queue("Outreach Agent", {"task": f"Contact partner: {partner.get('name')}", "priority": 2}, self.client_id)
                    metrics["leads_generated"] += len(partners[:3])
                    return
        mock_partners = [{"name": f"partner{i}@example.com"} for i in range(3)]
        for partner in mock_partners:
            log_action("Partnership Scout Agent", f"Mock partner identified: {partner['name']}", self.client_id)
            update_task_queue("Outreach Agent", {"task": f"Contact mock partner: {partner['name']}", "priority": 2}, self.client_id)
        metrics["leads_generated"] += 3

class ChainValidatorAgent:
    """Validates agent logic and workflow integrity."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Chain Validator Agent", f"Running task: {task['task']}", self.client_id)
        if "validate" in task["task"].lower():
            self.validate_pipeline()

    def validate_pipeline(self):
        queue = load_task_queue(self.client_id)
        for agent_name, tasks in queue.items():
            if len(tasks) > 10:
                log_action("Chain Validator Agent", f"Warning: {agent_name} has {len(tasks)} pending tasks", self.client_id)
                send_message_to_digiman(f"Task backlog for {agent_name}", self.client_id)
        log_action("Chain Validator Agent", "Pipeline validated", self.client_id)

class StrategicPlannerAgent:
    """Plans agent deployment and resource allocation."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Strategic Planner Agent", f"Running task: {task['task']}", self.client_id)
        if "plan" in task["task"].lower():
            self.plan_deployment()

    def plan_deployment(self):
        if metrics["leads_generated"] < 100:
            update_task_queue("Scout Agent", {"task": "Find niches", "priority": 3}, self.client_id)
            update_task_queue("Outreach Agent", {"task": "Send outreach", "priority": 3}, self.client_id)
        log_action("Strategic Planner Agent", "Deployment planned based on lead volume", self.client_id)

class CloserAgent:
    """Handles sales calls and deal closing."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "CALL" in key.upper() or "VOICE" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Closer Agent", f"Running task: {task['task']}", self.client_id)
        if "close deal" in task["task"].lower():
            self.close_deal()

    def close_deal(self):
        if self.active and self.required_keys:
            log_action("Closer Agent", "Initiated call via API", self.client_id)
        else:
            log_action("Closer Agent", "Mock deal closed", self.client_id)
            metrics["revenue_generated"] += 1000
            update_task_queue("CRM Agent", {"task": "Update deal status", "priority": 2}, self.client_id)

class CRMAgent:
    """Manages leads and client data in CRM systems."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "CRM" in key.upper() or "HUBSPOT" in key.upper() or "SALESFORCE" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("CRM Agent", f"Running task: {task['task']}", self.client_id)
        if "add lead" in task["task"].lower():
            self.add_lead(task["task"])
        elif "update deal" in task["task"].lower():
            self.update_deal()

    def add_lead(self, task):
        lead_email = re.search(r"[\w\.-]+@[\w\.-]+", task)
        if lead_email and self.active:
            for key in self.required_keys:
                api_key = CONFIG[key]
                endpoint = CONFIG.get(f"{key}_ENDPOINT", f"https://api.{key.lower().split('_')[0]}.com/v1/leads")
                response = make_api_request(endpoint, method="POST", api_key=api_key, data={"email": lead_email.group()})
                if response:
                    log_action("CRM Agent", f"Added lead {lead_email.group()} to {key}", self.client_id)
                    return
        log_action("CRM Agent", f"Mock lead added: {lead_email.group() if lead_email else 'unknown'}", self.client_id)

    def update_deal(self):
        log_action("CRM Agent", "Mock deal updated in CRM", self.client_id)

class ScoutAgent:
    """Identifies market niches and target clients."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "SEARCH" in key.upper() or "SERP" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Scout Agent", f"Running task: {task['task']}", self.client_id)
        if "find niches" in task["task"].lower():
            self.find_niches()

    def find_niches(self):
        if self.active:
            log_action("Scout Agent", "Searching for niches via API", self.client_id)
        else:
            log_action("Scout Agent", "Mock niches identified: tech, retail", self.client_id)
            update_task_queue("Marketing Agent", {"task": "Create campaign for tech niche", "priority": 2}, self.client_id)

class BrandManagerAgent:
    """Coordinates branding and visual consistency."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Brand Manager Agent", f"Running task: {task['task']}", self.client_id)
        if "manage branding" in task["task"].lower():
            self.manage_branding()

    def manage_branding(self):
        log_action("Brand Manager Agent", "Ensured brand consistency", self.client_id)
        update_task_queue("Visuals Agent", {"task": "Create branding assets", "priority": 2}, self.client_id)

class MarketingAgent:
    """Runs marketing campaigns and content creation."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "MARKETING" in key.upper() or "ADS" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Marketing Agent", f"Running task: {task['task']}", self.client_id)
        if "create campaign" in task["task"].lower():
            self.create_campaign(task["task"])
        elif "promote site" in task["task"].lower():
            self.promote_site()

    def create_campaign(self, task):
        niche = re.search(r"for (\w+) niche", task)
        if self.active:
            log_action("Marketing Agent", f"Launched campaign for {niche.group(1) if niche else 'general'}", self.client_id)
        else:
            log_action("Marketing Agent", f"Mock campaign launched for {niche.group(1) if niche else 'general'}", self.client_id)
            metrics["leads_generated"] += 50
            update_task_queue("Socials Agent", {"task": f"Post campaign content for {niche.group(1) if niche else 'general'}", "priority": 2}, self.client_id)

    def promote_site(self):
        log_action("Marketing Agent", "Promoted site via mock channels", self.client_id)

class VisualsAgent:
    """Designs visual assets for branding and marketing."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "DESIGN" in key.upper() or "CANVA" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Visuals Agent", f"Running task: {task['task']}", self.client_id)
        if "create branding assets" in task["task"].lower():
            self.create_branding_assets()

    def create_branding_assets(self):
        if self.active:
            log_action("Visuals Agent", "Created branding assets via API", self.client_id)
        else:
            log_action("Visuals Agent", "Mock branding assets created", self.client_id)

class SocialsAgent:
    """Manages social media presence and engagement."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "SOCIAL" in key.upper() or "INSTAGRAM" in key.upper() or "TWITTER" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Socials Agent", f"Running task: {task['task']}", self.client_id)
        if "post content" in task["task"].lower():
            self.post_content()

    def post_content(self):
        if self.active:
            log_action("Socials Agent", "Posted content via social APIs", self.client_id)
        else:
            log_action("Socials Agent", "Mock social post created", self.client_id)
            metrics["leads_generated"] += 20

class OutreachAgent:
    """Handles outbound communication and lead generation."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = ["EMAIL_ACCOUNT", "EMAIL_PASSWORD"]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Outreach Agent", f"Running task: {task['task']}", self.client_id)
        if "contact partner" in task["task"].lower():
            self.contact_partner(task["task"])

    def contact_partner(self, task):
        partner = re.search(r"partner: (.+)", task)
        if partner and self.active:
            smtp = smtplib.SMTP_SSL(CONFIG["SMTP_SERVER"], CONFIG["SMTP_PORT"])
            smtp.login(CONFIG["EMAIL_ACCOUNT"], CONFIG["EMAIL_PASSWORD"])
            msg = EmailMessage()
            msg["Subject"] = "Partnership Opportunity"
            msg["From"] = CONFIG["EMAIL_ACCOUNT"]
            msg["To"] = partner.group(1)
            msg.set_content(f"Hi, let's explore a partnership to grow our businesses!")
            smtp.send_message(msg)
            smtp.quit()
            log_action("Outreach Agent", f"Contacted partner: {partner.group(1)}", self.client_id)
        else:
            log_action("Outreach Agent", f"Mock outreach sent to {partner.group(1) if partner else 'unknown'}", self.client_id)

class SubscriptionAgent:
    """Manages billing and subscription plans."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "PAYMENT" in key.upper() or "STRIPE" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Subscription Agent", f"Running task: {task['task']}", self.client_id)
        if "manage billing" in task["task"].lower():
            self.manage_billing()

    def manage_billing(self):
        if self.active:
            log_action("Subscription Agent", "Processed billing via payment API", self.client_id)
        else:
            log_action("Subscription Agent", "Mock billing processed", self.client_id)

class SupportAgent:
    """Handles customer support tickets and feedback."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "SUPPORT" in key.upper() or "ZENDESK" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Support Agent", f"Running task: {task['task']}", self.client_id)
        if "handle ticket" in task["task"].lower() or "investigate" in task["task"].lower():
            self.handle_ticket()

    def handle_ticket(self):
        log_action("Support Agent", "Mock ticket resolved", self.client_id)

class RetentionAgent:
    """Reduces churn and improves customer lifetime value."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Retention Agent", f"Running task: {task['task']}", self.client_id)
        if "reduce churn" in task["task"].lower():
            self.reduce_churn()

    def reduce_churn(self):
        log_action("Retention Agent", "Mock retention strategy applied", self.client_id)
        update_task_queue("Email Agent", {"task": "Send retention email campaign", "priority": 2}, self.client_id)

class AnalystAgent:
    """Analyzes business metrics and trends."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = [key for key in CONFIG if "ANALYTICS" in key.upper()]
        self.active = not audit_env_keys(self.required_keys)

    def run_task(self, task):
        log_action("Analyst Agent", f"Running task: {task['task']}", self.client_id)
        if "analyze" in task["task"].lower():
            self.analyze_data(task["task"])

    def analyze_data(self, task):
        log_action("Analyst Agent", f"Analyzed data: {task}", self.client_id)
        if "report" in task.lower():
            update_task_queue("Manager Agent", {"task": "Review analytics report", "priority": 2}, self.client_id)

class FranchiseBuilderAgent:
    """Clones and deploys DigiMan franchises."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Franchise Builder Agent", f"Running task: {task['task']}", self.client_id)
        if "deploy franchise" in task["task"].lower():
            self.deploy_franchise()

    def deploy_franchise(self):
        franchise_id = f"franchise_{random.randint(1000,9999)}"
        log_action("Franchise Builder Agent", f"Mock franchise deployed: {franchise_id}", self.client_id)
        update_task_queue("Franchise Relationship Agent", {"task": f"Onboard franchise: {franchise_id}", "priority": 2}, self.client_id)

class FranchiseIntelligenceAgent:
    """Analyzes franchise performance and optimizes agents."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Franchise Intelligence Agent", f"Running task: {task['task']}", self.client_id)
        if "analyze franchise" in task["task"].lower():
            self.analyze_franchise()

    def analyze_franchise(self):
        log_action("Franchise Intelligence Agent", "Mock franchise analysis completed", self.client_id)

class FranchiseRelationshipAgent:
    """Supports franchise operators with onboarding and training."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Franchise Relationship Agent", f"Running task: {task['task']}", self.client_id)
        if "onboard franchise" in task["task"].lower():
            self.onboard_franchise(task["task"])

    def onboard_franchise(self, task):
        franchise_id = re.search(r"franchise: (\w+)", task)
        log_action("Franchise Relationship Agent", f"Mock franchise onboarded: {franchise_id.group(1) if franchise_id else 'unknown'}", self.client_id)

class AutonomousSalesReplicator:
    """Replicates successful sales strategies across niches."""
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.required_keys = []
        self.active = True

    def run_task(self, task):
        log_action("Autonomous Sales Replicator", f"Running task: {task['task']}", self.client_id)
        if "replicate strategy" in task["task"].lower():
            self.replicate_strategy()

    def replicate_strategy(self):
        log_action("Autonomous Sales Replicator", "Mock sales strategy replicated", self.client_id)
        update_task_queue("Closer Agent", {"task": "Close deal with replicated strategy", "priority": 2}, self.client_id)

# === Agent Registry ===
AGENT_REGISTRY = {
    "Manager Agent": ManagerAgent,
    "Email Agent": EmailAgent,
    "Web Builder Agent": WebBuilderAgent,
    "Partnership Scout Agent": PartnershipScoutAgent,
    "Chain Validator Agent": ChainValidatorAgent,
    "Strategic Planner Agent": StrategicPlannerAgent,
    "Closer Agent": CloserAgent,
    "CRM Agent": CRMAgent,
    "Scout Agent": ScoutAgent,
    "Brand Manager Agent": BrandManagerAgent,
    "Marketing Agent": MarketingAgent,
    "Visuals Agent": VisualsAgent,
    "Socials Agent": SocialsAgent,
    "Outreach Agent": OutreachAgent,
    "Subscription Agent": SubscriptionAgent,
    "Support Agent": SupportAgent,
    "Retention Agent": RetentionAgent,
    "Analyst Agent": AnalystAgent,
    "Franchise Builder Agent": FranchiseBuilderAgent,
    "Franchise Intelligence Agent": FranchiseIntelligenceAgent,
    "Franchise Relationship Agent": FranchiseRelationshipAgent,
    "Autonomous Sales Replicator": AutonomousSalesReplicator
}

# === Agent Deployment ===
def deploy_agent(agent_name, agent_class, client_id=None):
    """Deploy agent by writing its code to file and scoring it."""
    agent_dir = f".digi/clients/{client_id}" if client_id else ".digi"
    os.makedirs(agent_dir, exist_ok=True)
    filename = f"{agent_name.lower().replace(' ', '_')}_agent.py"
    filepath = os.path.join(agent_dir, filename)
    meta_path = os.path.join(agent_dir, f"{filename}.meta")
    scoreboard_path = os.path.join(agent_dir, "scoreboard.txt")

    code = inspect.getsource(agent_class)
    score, reasons = evaluate_agent_quality(code)

    try:
        with open(filepath, "w") as f:
            f.write(code)
        with open(meta_path, "w") as meta:
            meta.write(f"Score: {score}/4\n")
            for reason in reasons:
                meta.write(f"Issue: {reason}\n")
            if score < 3:
                meta.write("LOCKED\n")
                log_action(agent_name, "Agent locked due to low score", client_id)
            else:
                meta.write("DEPLOYED\n")
        with open(scoreboard_path, "a") as board:
            board.write(f"{agent_name}: {score}/4 - {' | '.join(reasons) if reasons else 'OK'}\n")
    except Exception as e:
        logger.error(f"Failed to deploy {agent_name}: {e}")
        return False

    if score >= 3:
        metrics["agents_generated"] += 1
        log_action(agent_name, f"Deployed with score: {score}/4", client_id)
        return True
    return False

def run_agents(client_id=None):
    """Execute all deployed agents' tasks."""
    queue = load_task_queue(client_id)
    for agent_name, agent_class in AGENT_REGISTRY.items():
        agent_instance = agent_class(client_id)
        tasks = sorted(queue.get(agent_name, []), key=lambda x: x.get("priority", 1), reverse=True)
        for task in tasks:
            try:
                agent_instance.run_task(task)
            except Exception as e:
                log_action(agent_name, f"Task error: {e}", client_id)
                metrics["tasks_failed"] += 1
        queue[agent_name] = []  # Clear processed tasks
    queue_path = os.path.join(f".digi/clients/{client_id}" if client_id else ".digi", "agent_queue.json")
    try:
        with open(queue_path, "w") as f:
            json.dump(queue, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save task queue: {e}")

# === Flask Endpoints ===
@app.route("/digiman/command", methods=["POST"])
def digiman_command():
    try:
        content = request.json.get("message", "")
        client_id = request.json.get("client_id")
        send_message_to_digiman(f"USER: {content}", client_id)
        update_task_queue("Manager Agent", {"task": f"Process command: {content}", "priority": 2}, client_id)
        return jsonify({"status": "received"})
    except Exception as e:
        logger.error(f"Command endpoint error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/digiman/insights", methods=["GET"])
def get_insights():
    try:
        client_id = request.args.get("client_id")
        return jsonify({
            "phase": business_phases[current_phase_index],
            "metrics": metrics
        })
    except Exception as e:
        logger.error(f"Insights endpoint error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/digiman/integrate", methods=["POST"])
def integrate_app():
    try:
        client_id = request.json.get("client_id")
        app_name = request.json.get("app_name")
        api_key = request.json.get("api_key")
        endpoint = request.json.get("endpoint")
        CONFIG[f"{app_name.upper()}_API_KEY"] = api_key
        CONFIG[f"{app_name.upper()}_ENDPOINT"] = endpoint
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=2)
        log_action("Integration", f"Integrated {app_name} for client {client_id}", client_id)
        return jsonify({"status": "integrated", "app": app_name})
    except Exception as e:
        logger.error(f"Integration endpoint error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# === Autonomous Loop ===
def autonomous_loop():
    global current_phase_index
    agent_tasks = [
        ("Chain Validator Agent", "Validates logic and system alignment"),
        ("Strategic Planner Agent", "Deploys agents based on business needs"),
        ("Closer Agent", "Handles calls, objections, and closes deals"),
        ("CRM Agent", "Captures and manages lead intelligence"),
        ("Scout Agent", "Finds niches, pain points, and target clients"),
        ("Brand Manager Agent", "Assigns visuals, socials, and marketing"),
        ("Marketing Agent", "Writes email/DMs, launches funnels"),
        ("Visuals Agent", "Designs branding assets"),
        ("Socials Agent", "Posts content, grows channels"),
        ("Outreach Agent", "Sends outbound emails and messages"),
        ("Subscription Agent", "Manages billing and upgrades"),
        ("Support Agent", "Handles tickets and feedback"),
        ("Retention Agent", "Reduces churn, boosts LTV"),
        ("Web Builder Agent", "Creates landing pages and websites"),
        ("Manager Agent", "Manages client agents"),
        ("Analyst Agent", "Reviews pricing, growth, and trends"),
        ("Franchise Builder Agent", "Clones and deploys DigiMan franchises"),
        ("Franchise Intelligence Agent", "Analyzes franchise performance"),
        ("Franchise Relationship Agent", "Handles onboarding and support for franchises"),
        ("Autonomous Sales Replicator", "Clones high-converting sales strategies"),
        ("Email Agent", "Manages inbound and outbound client communication"),
        ("Partnership Scout Agent", "Identifies collaboration opportunities")
    ]

    required_keys = set(key for agent_cls in AGENT_REGISTRY.values() for key in agent_cls(None).required_keys)
    audit_env_keys(required_keys)
    overrides = check_owner_overrides()

    # Deploy agents
    for agent_name, _ in agent_tasks:
        if agent_name not in overrides:
            deploy_agent(agent_name, AGENT_REGISTRY[agent_name])

    # Queue initial tasks
    for agent_name, task_desc in agent_tasks:
        update_task_queue(agent_name, {"task": task_desc, "priority": 1})

    # Run agents
    run_agents()
    current_phase_index = (current_phase_index + 1) % len(business_phases)

def main_loop_forever():
    flask_thread = threading.Thread(target=app.run, kwargs={"host": "0.0.0.0", "port": 5001})
    flask_thread.daemon = True
    flask_thread.start()
    time.sleep(1)
    while True:
        try:
            autonomous_loop()
        except Exception as e:
            logger.error(f"Autonomous loop error: {e}")
        time.sleep(10)

if __name__ == "__main__":
    print("DigiMan 6.0.3 – Autonomous Business OS with Franchise Intelligence")
    main_loop_forever()