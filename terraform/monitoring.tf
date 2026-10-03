# Dashboard and alert policies are defined in ../monitoring so they can be
# reviewed (and edited in the console, then exported) as plain JSON/YAML.

locals {
  monitoring_dir = "${path.module}/../monitoring"
  monitoring_vars = {
    cluster_name   = var.cluster_name
    namespace      = var.k8s_namespace
    max_node_count = var.max_node_count
    node_pool      = "ml-pool" # google_container_node_pool.ml_pool.name
  }

  all_alert_policies = {
    for file in fileset("${local.monitoring_dir}/alerts", "*.yaml") :
    trimsuffix(file, ".yaml") => yamldecode(templatefile("${local.monitoring_dir}/alerts/${file}", local.monitoring_vars))
  }

  # Alerts on TF Serving metrics can only be created once those metrics exist.
  alert_policies = {
    for name, alert in local.all_alert_policies : name => alert
    if var.enable_workload_alerts || !strcontains(alert.query, "tfserving_")
  }

  notification_channels = var.alert_email == "" ? [] : [google_monitoring_notification_channel.email[0].id]
}

resource "google_monitoring_dashboard" "serving" {
  count = var.enable_monitoring ? 1 : 0

  dashboard_json = templatefile("${local.monitoring_dir}/dashboards/resnet-serving.json", local.monitoring_vars)

  depends_on = [google_project_service.services]
}

resource "google_monitoring_notification_channel" "email" {
  count = var.enable_monitoring && var.alert_email != "" ? 1 : 0

  display_name = "ResNet serving alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_alert_policy" "serving" {
  for_each = var.enable_monitoring ? local.alert_policies : {}

  display_name          = each.value.displayName
  severity              = each.value.severity
  combiner              = "OR"
  notification_channels = local.notification_channels

  conditions {
    display_name = each.value.displayName

    condition_prometheus_query_language {
      query               = each.value.query
      duration            = each.value.duration
      evaluation_interval = "60s"
    }
  }

  documentation {
    content   = each.value.documentation
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }

  depends_on = [google_project_service.services]
}
