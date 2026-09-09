from django.apps import AppConfig
 
 
class RestauranteConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'restaurante_app'

    def ready(self):
        from . import signals  # noqa: F401
 