from django.shortcuts import render
from django.http import HttpResponse

def index(request):
    return HttpResponse("Bienvenido a exportador")


# Create your views here.
