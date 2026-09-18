from django.urls import path
from . import views

urlpatterns = [
    # Autenticação
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('cadastro/', views.cadastro_view, name='cadastro'),

    # Home
    path('', views.home, name='home'),

    # Estoque (produtos)
    path('produtos/', views.produto_list, name='produto_list'),
    path('produtos/novo/', views.produto_create, name='produto_create'),
    path('produtos/<int:pk>/editar/', views.produto_update, name='produto_update'),
    path('produtos/<int:pk>/excluir/', views.produto_delete, name='produto_delete'),

    # Histórico de movimentações de estoque
    path('estoque/historico/', views.estoque_historico, name='estoque_historico'),
    path('estoque/movimentar/', views.movimentacao_create, name='movimentacao_create'),

    # Ficha técnica / cardápio
    path('pratos/', views.prato_list, name='prato_list'),
    path('pratos/novo/', views.prato_create, name='prato_create'),
    path('pratos/<int:pk>/editar/', views.prato_update, name='prato_update'),
    path('pratos/<int:pk>/excluir/', views.prato_delete, name='prato_delete'),
    path('pratos/<int:pk>/vender/', views.prato_vender, name='prato_vender'),

    # Fornecedores (compras)
    path('fornecedores/', views.fornecedor_list, name='fornecedor_list'),
    path('fornecedores/novo/', views.fornecedor_create, name='fornecedor_create'),
    path('fornecedores/<int:pk>/editar/', views.fornecedor_update, name='fornecedor_update'),
    path('fornecedores/<int:pk>/excluir/', views.fornecedor_delete, name='fornecedor_delete'),

    # Controle de agendamento (visão geral)
    path('reservas/', views.reserva_list, name='reserva_list'),
    path('reservas/<int:pk>/cancelar/', views.reserva_delete, name='reserva_delete'),
    path('reservas/<int:pk>/pagar/', views.reserva_marcar_pago, name='reserva_marcar_pago'),

    # Reserva de mesa
    path('reservar/', views.reserva_create, name='reserva_create'),

    # Financeiro
    path('financeiro/', views.financeiro_view, name='financeiro'),

    # Despesas fixas (aluguel, água, energia, outros)
    path('despesas/', views.despesa_list, name='despesa_list'),
    path('despesas/nova/', views.despesa_create, name='despesa_create'),
    path('despesas/<int:pk>/editar/', views.despesa_update, name='despesa_update'),
    path('despesas/<int:pk>/excluir/', views.despesa_delete, name='despesa_delete'),

     #Conectar esp32
    path('temperatura/', views.temperatura_view, name='temperatura'),
    path('api/temperatura/', views.receber_temperatura, name='api_temperatura'),

    # Notificações (banner de alertas)
    path('api/notificacoes/', views.notificacoes_ativas, name='notificacoes_ativas'),

    # Painel de usuários (acesso restrito por usuário e senha) — ver e excluir contas
    path('painel-usuarios/', views.painel_usuarios_login, name='login_adm'),
    path('painel-usuarios/sair/', views.painel_usuarios_logout, name='painel_usuarios_logout'),
    path('painel-usuarios/lista/', views.painel_usuarios_list, name='painel_usuarios_list'),
    path('painel-usuarios/<int:pk>/excluir/', views.painel_usuarios_delete, name='painel_usuarios_delete'),
]
