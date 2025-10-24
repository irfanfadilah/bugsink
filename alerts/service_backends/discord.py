import json
import requests
from django.utils import timezone

from django import forms
from django.template.defaultfilters import truncatechars

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings
from bugsink.transaction import immediate_atomic

from issues.models import Issue


class DiscordConfigForm(forms.Form):
    webhook_url = forms.URLField(required=True)

    def __init__(self, *args, **kwargs):
        config = kwargs.pop("config", None)

        super().__init__(*args, **kwargs)
        if config:
            self.fields["webhook_url"].initial = config.get("webhook_url", "")

    def get_config(self):
        return {
            "webhook_url": self.cleaned_data.get("webhook_url"),
        }


def _store_failure_info(service_config_id, exception, response=None):
    """Store failure information in the MessagingServiceConfig with immediate_atomic"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)

            config.last_failure_timestamp = timezone.now()
            config.last_failure_error_type = type(exception).__name__
            config.last_failure_error_message = str(exception)

            if response is not None:
                config.last_failure_status_code = response.status_code
                config.last_failure_response_text = response.text[:2000]

                try:
                    json.loads(response.text)
                    config.last_failure_is_json = True
                except (json.JSONDecodeError, ValueError):
                    config.last_failure_is_json = False
            else:
                config.last_failure_status_code = None
                config.last_failure_response_text = None
                config.last_failure_is_json = None

            config.save()
        except MessagingServiceConfig.DoesNotExist:
            pass


def _store_success_info(service_config_id):
    """Clear failure information on successful operation"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)
            config.clear_failure_status()
            config.save()
        except MessagingServiceConfig.DoesNotExist:
            pass


@shared_task
def discord_backend_send_test_message(webhook_url, project_name, display_name, service_config_id):
    issue_url = get_settings().BASE_URL + "/issues/issue/00000000-0000-0000-0000-000000000000/"

    data = {
        "content": "**TEST issue**",
        "embeds": [{
            "title": "Test message by Bugsink to test the webhook setup.",
            "url": issue_url,
            "color": 3447003,
            "fields": [
                {
                    "name": "Project",
                    "value": project_name,
                    "inline": True
                },
                {
                    "name": "Message backend",
                    "value": display_name,
                    "inline": True
                }
            ]
        }]
    }

    try:
        result = requests.post(
            webhook_url,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )

        result.raise_for_status()

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, 'response', None)
        _store_failure_info(service_config_id, e, response)

    except Exception as e:
        _store_failure_info(service_config_id, e)


@shared_task
def discord_backend_send_alert(
        webhook_url, issue_id, state_description, alert_article, alert_reason, service_config_id, unmute_reason=None):

    issue = Issue.objects.get(id=issue_id)

    issue_url = get_settings().BASE_URL + issue.get_absolute_url()
    issue_title = truncatechars(issue.title(), 200)

    color_map = {
        "NEW": 15158332,
        "REGRESSED": 15105570,
        "UNMUTED": 15844367
    }
    color = color_map.get(alert_reason, 3447003)

    fields = [
        {
            "name": "Project",
            "value": issue.project.name,
            "inline": True
        }
    ]

    description_parts = []
    if unmute_reason:
        description_parts.append(unmute_reason)

    data = {
        "content": f"**{alert_reason} issue**",
        "embeds": [{
            "title": issue_title,
            "url": issue_url,
            "color": color,
            "fields": fields
        }]
    }

    if description_parts:
        data["embeds"][0]["description"] = "\n".join(description_parts)

    try:
        result = requests.post(
            webhook_url,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )

        result.raise_for_status()

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, 'response', None)
        _store_failure_info(service_config_id, e, response)

    except Exception as e:
        _store_failure_info(service_config_id, e)


class DiscordBackend:

    def __init__(self, service_config):
        self.service_config = service_config

    def get_form_class(self):
        return DiscordConfigForm

    def send_test_message(self):
        discord_backend_send_test_message.delay(
            json.loads(self.service_config.config)["webhook_url"],
            self.service_config.project.name,
            self.service_config.display_name,
            self.service_config.id,
        )

    def send_alert(self, issue_id, state_description, alert_article, alert_reason, **kwargs):
        discord_backend_send_alert.delay(
            json.loads(self.service_config.config)["webhook_url"],
            issue_id, state_description, alert_article, alert_reason, self.service_config.id, **kwargs)
