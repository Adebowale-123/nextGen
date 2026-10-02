from django.urls import path

from . import views

app_name = "games"

urlpatterns = [
    path("play/", views.lobby, name="lobby"),
    path("play/draws/<int:pk>/", views.draw_detail, name="draw_detail"),
    path("play/<slug:slug>/", views.game_detail, name="game_detail"),
    path("play/<slug:slug>/play/", views.play_game, name="play_game"),
    path("tickets/", views.my_tickets, name="my_tickets"),
    path("tickets/<str:serial>/", views.ticket_detail, name="ticket_detail"),
    path("results/", views.results, name="results"),
    path("results/<int:pk>/", views.result_detail, name="result_detail"),
]
