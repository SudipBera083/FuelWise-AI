from django.urls import path
from .views import OptimizeRouteView

urlpatterns = [
    path('optimize-route/', OptimizeRouteView.as_view(), name='optimize_route'),
]
