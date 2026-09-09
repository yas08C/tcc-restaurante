from django.contrib import admin
from .models import Produto, Reserva, Fornecedor
 
admin.site.register(Produto)
admin.site.register(Reserva)
admin.site.register(Fornecedor)
from django.contrib import admin
from .models import ZonaTemperatura, SensorESP32, MovimentacaoEstoque, Prato, ItemFichaTecnica

admin.site.register(ZonaTemperatura)
admin.site.register(SensorESP32)
admin.site.register(MovimentacaoEstoque)
admin.site.register(Prato)
admin.site.register(ItemFichaTecnica)
