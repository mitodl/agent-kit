# Excerpt of mitodl/ol-infrastructure
# src/ol_infrastructure/applications/example_reports/__main__.py, the gateway
# and service definitions for the example-reports app.

from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
)
from pulumi import ResourceOptions
from pulumi_kubernetes.core.v1 import Service, ServicePortArgs, ServiceSpecArgs

APPLICATION_NAMESPACE = "example-reports"

# ClusterIP only: nothing outside the cluster reaches the pods except through
# the APISIX routes below.
example_reports_service = Service(
    "example-reports-service",
    metadata={"name": "example-reports", "namespace": APPLICATION_NAMESPACE},
    spec=ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "example-reports"},
        ports=[ServicePortArgs(name="http", port=8000, target_port=8000)],
    ),
)

example_reports_oidc = OLApisixOIDCResources(
    "example-reports-oidc",
    oidc_config=OLApisixOIDCConfig(
        application_name="example-reports",
        k8s_namespace=APPLICATION_NAMESPACE,
    ),
)

# Partner API: bearer tokens only. A request with no valid Keycloak token gets
# a 401 from APISIX and never reaches the app. On success the plugin replaces
# any client-supplied X-Userinfo header with the token's claims.
_reports_api_plugin = example_reports_oidc.get_full_oidc_plugin_config(
    unauth_action="deny"
)
_reports_api_plugin["config"].update(
    {
        "bearer_only": True,
        "use_jwks": True,
        "set_userinfo_header": True,
        "required_scopes": ["reports:read"],
    }
)

example_reports_route = OLApisixRoute(
    "example-reports-route",
    k8s_namespace=APPLICATION_NAMESPACE,
    route_configs=[
        OLApisixRouteConfig(
            route_name="example-reports-api",
            priority=20,
            plugins=[OLApisixPluginConfig(**_reports_api_plugin)],
            hosts=["reports.example.mit.edu"],
            paths=["/api/v1/reports", "/api/v1/reports/*"],
            backend_service_name="example-reports",
            backend_service_port="http",
        ),
        OLApisixRouteConfig(
            route_name="example-reports-health",
            priority=10,
            plugins=[],
            hosts=["reports.example.mit.edu"],
            paths=["/health"],
            backend_service_name="example-reports",
            backend_service_port="http",
        ),
    ],
    opts=ResourceOptions(depends_on=[example_reports_service, example_reports_oidc]),
)
