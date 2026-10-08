from django.contrib import admin
from .models import (
    Produto, Reserva, Fornecedor, ZonaTemperatura, SensorESP32,
    MovimentacaoEstoque, Prato, ItemFichaTecnica, Pedido, ItemPedido,
)

admin.site.register(Produto)
admin.site.register(Reserva)
admin.site.register(Fornecedor)
admin.site.register(ZonaTemperatura)
admin.site.register(MovimentacaoEstoque)
admin.site.register(Prato)
admin.site.register(ItemFichaTecnica)
admin.site.register(Pedido)
admin.site.register(ItemPedido)


@admin.register(SensorESP32)
class SensorESP32Admin(admin.ModelAdmin):
    list_display = ('nome', 'usuario', 'tipo_sensor', 'zona', 'ativo', 'criado_em')
    list_filter = ('usuario', 'tipo_sensor', 'zona', 'ativo')
    readonly_fields = ('token', 'criado_em')