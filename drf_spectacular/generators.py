import inspect
from urllib.parse import urljoin

from django.urls import URLResolver, URLPattern
from rest_framework import viewsets, views
from rest_framework.compat import get_original_route
from rest_framework.schemas.generators import (
    BaseSchemaGenerator,
    EndpointEnumerator as BaseEndpointEnumerator,
)

from drf_spectacular.extensions import OpenApiViewExtension
from drf_spectacular.plumbing import (
    ComponentRegistry, error, alpha_operation_sorter, reset_generator_stats, build_root_object
)
from drf_spectacular.settings import spectacular_settings


class EndpointEnumerator(BaseEndpointEnumerator):
    def get_api_endpoints(self, patterns=None, prefix=''):
        """
        Return a list of all available API endpoints by inspecting the URL conf.
        """
        if patterns is None:
            patterns = self.patterns

        api_endpoints = []

        for pattern in patterns:
            path_regex = prefix + get_original_route(pattern)
            if isinstance(pattern, URLPattern):
                path = self.get_path_from_regex(path_regex)
                callback = pattern.callback
                if self.should_include_endpoint(path, callback):
                    for method in self.get_allowed_methods(callback):
                        endpoint = (path, path_regex, method, callback)
                        api_endpoints.append(endpoint)

            elif isinstance(pattern, URLResolver):
                nested_endpoints = self.get_api_endpoints(
                    patterns=pattern.url_patterns,
                    prefix=path_regex
                )
                api_endpoints.extend(nested_endpoints)

        return sorted(api_endpoints, key=alpha_operation_sorter)


class SchemaGenerator(BaseSchemaGenerator):
    endpoint_inspector_cls = EndpointEnumerator

    def __init__(self, *args, **kwargs):
        self.registry = ComponentRegistry()
        super().__init__(*args, **kwargs)

    def create_view(self, callback, method, request=None):
        """
        customized create_view which is called when all routes are traversed. part of this
        is instantiating views with default params. in case of custom routes (@action) the
        custom AutoSchema is injected properly through 'initkwargs' on view. However, when
        decorating plain views like retrieve, this initialization logic is not running.
        Therefore forcefully set the schema if @extend_schema decorator was used.
        """
        override_view = OpenApiViewExtension.get_match(callback.cls)
        if override_view:
            callback.cls = override_view.view_replacement()

        view = super().create_view(callback, method, request)

        if isinstance(view, viewsets.GenericViewSet) or isinstance(view, viewsets.ViewSet):
            action = getattr(view, view.action)
        elif isinstance(view, views.APIView):
            action = getattr(view, method.lower())
        else:
            error(
                'Using not supported View class. Class must be derived from APIView '
                'or any of its subclasses like GenericApiView, GenericViewSet.'
            )
            return view

        # in case of @extend_schema, manually init custom schema class here due to
        # weakref reverse schema.view bug for multi annotations.
        schema = getattr(action, 'kwargs', {}).get('schema', None)
        if schema and inspect.isclass(schema):
            view.schema = schema()

        return view

    def _get_paths_and_endpoints(self, request):
        """
        Generate (path, method, view) given (path, method, callback) for paths.
        """
        view_endpoints = []
        for path, path_regex, method, callback in self.endpoints:
            view = self.create_view(callback, method, request)
            path = self.coerce_path(path, method, view)
            view_endpoints.append((path, path_regex, method, view))

        return view_endpoints

    def parse(self, request=None):
        """ Iterate endpoints generating per method path operations. """
        result = {}
        self._initialise_endpoints()

        for path, path_regex, method, view in self._get_paths_and_endpoints(request):
            if not self.has_view_permissions(path, method, view):
                continue

            # beware that every access to schema yields a fresh object (descriptor pattern)
            operation = view.schema.get_operation(path, path_regex, method, self.registry)

            # operation was manually removed via @extend_schema
            if not operation:
                continue

            # Normalise path for any provided mount url.
            if path.startswith('/'):
                path = path[1:]
            path = urljoin(self.url or '/', path)

            result.setdefault(path, {})
            result[path][method.lower()] = operation

        return result

    def get_schema(self, request=None, public=False):
        """ Generate a OpenAPI schema. """
        reset_generator_stats()
        return build_root_object(
            paths=self.parse(None if public else request),
            components=self.registry.build(spectacular_settings.APPEND_COMPONENTS),
        )
