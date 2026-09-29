from django.contrib import admin
from .models import (
    Produto, Reserva, Fornecedor, ZonaTemperatura, SensorESP32,
    MovimentacaoEstoque, Prato, ItemFichaTecnica,
)

admin.site.register(Produto)
admin.site.register(Reserva)
admin.site.register(Fornecedor)
admin.site.register(ZonaTemperatura)
admin.site.register(MovimentacaoEstoque)
admin.site.register(Prato)
admin.site.register(ItemFichaTecnica)


@admin.register(SensorESP32)
class SensorESP32Admin(admin.ModelAdmin):
    list_display = ('nome', 'usuario', 'ativo', 'criado_em')
    readonly_fields = ('token', 'criado_em')
