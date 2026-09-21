from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.APIRootView = views.NetBoxDNSBridgeRootView

router.register("catalog-zones", views.CatalogZoneViewSet)
router.register("catalog-zone-members", views.CatalogZoneMemberViewSet)
router.register("seen-transfer-clients", views.SeenTransferClientViewSet)

urlpatterns = router.urls
