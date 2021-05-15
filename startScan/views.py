import os
import requests
import itertools

from datetime import datetime

from django.shortcuts import render, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse, HttpResponseRedirect, HttpResponse
from django.urls import reverse
from django_celery_beat.models import PeriodicTask, IntervalSchedule, ClockedSchedule
from django.utils import timezone
from django.conf import settings
from django.core import serializers

from startScan.models import *
from notification.models import NotificationHooks
from targetApp.models import Domain
from scanEngine.models import EngineType, Configuration
from reNgine.tasks import initiate_scan, create_scan_activity
from reNgine.celery import app

from reNgine.common_func import *


def scan_history(request):
    host = ScanHistory.objects.all().order_by('-start_scan_date')
    context = {'scan_history_active': 'true', "scan_history": host}
    return render(request, 'startScan/history.html', context)


def detail_scan(request, id=None):
    context = {}
    if id:
        context['scan_history_id'] = id
        context['subdomain_count'] = Subdomain.objects.filter(
            scan_history__id=id).values('name').distinct().count()
        context['alive_count'] = Subdomain.objects.filter(
            scan_history__id=id).values('name').distinct().exclude(
            http_status__exact=0).count()
        context['scan_activity'] = ScanActivity.objects.filter(
            scan_of__id=id).order_by('-time')
        context['endpoint_count'] = EndPoint.objects.filter(
            scan_history__id=id).values('http_url').distinct().count()
        context['endpoint_alive_count'] = EndPoint.objects.filter(
            scan_history__id=id, http_status__exact=200).values('http_url').distinct().count()
        context['history'] = get_object_or_404(ScanHistory, id=id)
        info_count = Vulnerability.objects.filter(
            scan_history__id=id, severity=0).count()
        low_count = Vulnerability.objects.filter(
            scan_history__id=id, severity=1).count()
        medium_count = Vulnerability.objects.filter(
            scan_history__id=id, severity=2).count()
        high_count = Vulnerability.objects.filter(
            scan_history__id=id, severity=3).count()
        critical_count = Vulnerability.objects.filter(
            scan_history__id=id, severity=4).count()
        context['vulnerability_list'] = Vulnerability.objects.filter(
            scan_history__id=id).order_by('-severity').all()[:20]
        context['total_vulnerability_count'] = info_count + low_count + \
            medium_count + high_count + critical_count
        context['info_count'] = info_count
        context['low_count'] = low_count
        context['medium_count'] = medium_count
        context['high_count'] = high_count
        context['critical_count'] = critical_count
        context['interesting_subdomain'] = get_interesting_subdomains(
            scan_history=id)
        context['interesting_endpoint'] = get_interesting_endpoint(
            scan_history=id)
        context['scan_history_active'] = 'true'

        domain_id = ScanHistory.objects.filter(id=id)[0].domain_name.id
        if ScanHistory.objects.filter(
            domain_name=domain_id).filter(
            scan_type__subdomain_discovery=True).filter(
            id__lt=id).filter(
                scan_status=2).count() > 1:

            last_scan = ScanHistory.objects.filter(
                domain_name=domain_id).filter(
                scan_type__subdomain_discovery=True).filter(
                scan_status=2).filter(
                id__lt=id).order_by('-start_scan_date')[0]

            scanned_host_q1 = Subdomain.objects.filter(
                scan_history__id=id).values('name')
            scanned_host_q2 = Subdomain.objects.filter(
                scan_history__id=last_scan.id).values('name')

            context['new_subdomains'] = scanned_host_q1.difference(
                scanned_host_q2)
            context['removed_subdomains'] = scanned_host_q2.difference(
                scanned_host_q1)
            context['last_scan_subdomain'] = last_scan

        if ScanHistory.objects.filter(
            domain_name=domain_id).filter(
            scan_type__fetch_url=True).filter(
            scan_status=2).filter(
                id__lt=id).count() > 1:

            last_scan = ScanHistory.objects.filter(
                domain_name=domain_id).filter(
                scan_type__fetch_url=True).filter(
                scan_status=2).filter(
                id__lt=id).order_by('-start_scan_date')[0]

            endpoint_q1 = EndPoint.objects.filter(
                scan_history__id=id).values('http_url')
            endpoint_q2 = EndPoint.objects.filter(
                scan_history__id=last_scan.id).values('http_url')

            context['new_urls'] = endpoint_q1.difference(endpoint_q2)
            context['removed_urls'] = endpoint_q2.difference(endpoint_q1)
            context['last_scan_endpoint'] = last_scan

        context['ip_addresses'] = IPAddress.objects.filter(scan_history=id).values_list('address', 'is_cdn').distinct().order_by()
        context['ports'] = Port.objects.filter(scan_history=id).values_list('number', 'service_name', 'description', 'is_uncommon').distinct().order_by('number')

    return render(request, 'startScan/detail_scan.html', context)

def get_ports_for_ip(request, ip, history_id):
    ports = Port.objects.filter(ip__address=ip).filter(scan_history=history_id).values_list('number', 'service_name', 'description', 'is_uncommon').distinct().order_by('number')
    return JsonResponse(json.dumps(list(ports)), safe=False)

def detail_vuln_scan(request, id=None):
    if id:
        history = get_object_or_404(ScanHistory, id=id)
        context = {'scan_history_id': id, 'history': history}
    else:
        context = {'vuln_scan_active': 'true'}
    return render(request, 'startScan/detail_vuln_scan.html', context)


def detail_endpoint_scan(request, id=None):
    if id:
        history = get_object_or_404(ScanHistory, id=id)
        context = {'scan_history_id': id, 'history': history}
    else:
        context = {}
    return render(request, 'startScan/detail_endpoint_scan.html', context)


def start_scan_ui(request, host_id):
    domain = get_object_or_404(Domain, id=host_id)
    if request.method == "POST":
        # get engine type
        engine_type = request.POST['scan_mode']
        scan_history_id = create_scan_object(host_id, engine_type)
        # start the celery task
        celery_task = initiate_scan.apply_async(
            args=(host_id, scan_history_id, 0, engine_type))
        ScanHistory.objects.filter(
            id=scan_history_id).update(
            celery_id=celery_task.id)
        messages.add_message(
            request,
            messages.INFO,
            'Scan Started for ' +
            domain.domain_name)
        return HttpResponseRedirect(reverse('scan_history'))
    engine = EngineType.objects.order_by('id')
    custom_engine_count = EngineType.objects.filter(
        default_engine=False).count()
    context = {
        'scan_history_active': 'true',
        'domain': domain,
        'engines': engine,
        'custom_engine_count': custom_engine_count}
    return render(request, 'startScan/start_scan_ui.html', context)


def start_multiple_scan(request):
    # domain = get_object_or_404(Domain, id=host_id)
    domain_text = ""
    if request.method == "POST":
        if request.POST.get('scan_mode', 0):
            # if scan mode is available, then start the scan
            # get engine type
            engine_type = request.POST['scan_mode']
            list_of_domains = request.POST['list_of_domain_id']
            for domain_id in list_of_domains.split(","):
                # start the celery task
                scan_history_id = create_scan_object(domain_id, engine_type)
                celery_task = initiate_scan.apply_async(
                    args=(domain_id, scan_history_id, 0, engine_type))
                ScanHistory.objects.filter(
                    id=scan_history_id).update(
                    celery_id=celery_task.id)
            messages.add_message(
                request,
                messages.INFO,
                'Scan Started for multiple targets')
            return HttpResponseRedirect(reverse('scan_history'))
        else:
            # this else condition will have post request from the scan page
            # containing all the targets id
            list_of_domain_name = []
            list_of_domain_id = []
            for key, value in request.POST.items():
                if key != "style-2_length" and key != "csrfmiddlewaretoken":
                    domain = get_object_or_404(Domain, id=value)
                    list_of_domain_name.append(domain.domain_name)
                    list_of_domain_id.append(value)
            domain_text = ", ".join(list_of_domain_name)
            domain_ids = ",".join(list_of_domain_id)
    engine = EngineType.objects
    custom_engine_count = EngineType.objects.filter(
        default_engine=False).count()
    context = {
        'scan_history_active': 'true',
        'engines': engine,
        'domain_list': domain_text,
        'domain_ids': domain_ids,
        'custom_engine_count': custom_engine_count}
    return render(request, 'startScan/start_multiple_scan_ui.html', context)


def export_subdomains(request, scan_id):
    subdomain_list = Subdomain.objects.filter(scan_history__id=scan_id)
    domain_results = ScanHistory.objects.get(id=scan_id)
    response_body = ""
    for name in subdomain_list:
        response_body = response_body + name.name + "\n"
    response = HttpResponse(response_body, content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="subdomains_' + \
        domain_results.domain_name.domain_name + '_' + \
        str(domain_results.start_scan_date.date()) + '.txt"'
    return response


def export_endpoints(request, scan_id):
    endpoint_list = EndPoint.objects.filter(scan_history__id=scan_id)
    domain_results = ScanHistory.objects.get(id=scan_id)
    response_body = ""
    for endpoint in endpoint_list:
        response_body = response_body + endpoint.http_url + "\n"
    response = HttpResponse(response_body, content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="endpoints_' + \
        domain_results.domain_name.domain_name + '_' + \
        str(domain_results.start_scan_date.date()) + '.txt"'
    return response


def export_urls(request, scan_id):
    urls_list = Subdomain.objects.filter(scan_history__id=scan_id)
    domain_results = ScanHistory.objects.get(id=scan_id)
    response_body = ""
    for url in urls_list:
        if url.http_url:
            response_body = response_body + url.http_url + "\n"
    response = HttpResponse(response_body, content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="urls_' + \
        domain_results.domain_name.domain_name + '_' + \
        str(domain_results.start_scan_date.date()) + '.txt"'
    return response


def delete_scan(request, id):
    obj = get_object_or_404(ScanHistory, id=id)
    if request.method == "POST":
        delete_dir = obj.domain_name.domain_name + '_' + \
            str(datetime.strftime(obj.start_scan_date, '%Y_%m_%d_%H_%M_%S'))
        delete_path = settings.TOOL_LOCATION + 'scan_results/' + delete_dir
        os.system('rm -rf ' + delete_path)
        obj.delete()
        messageData = {'status': 'true'}
        messages.add_message(
            request,
            messages.INFO,
            'Scan history successfully deleted!')
    else:
        messageData = {'status': 'false'}
        messages.add_message(
            request,
            messages.INFO,
            'Oops! something went wrong!')
    return JsonResponse(messageData)


def stop_scan(request, id):
    if request.method == "POST":
        scan_history = get_object_or_404(ScanHistory, celery_id=id)
        # stop the celery task
        app.control.revoke(id, terminate=True, signal='SIGKILL')
        scan_history.scan_status = 3
        scan_history.save()
        try:
            last_activity = ScanActivity.objects.filter(
                scan_of=scan_history).order_by('-pk')[0]
            last_activity.status = 0
            last_activity.time = timezone.now()
            last_activity.save()
        except Exception as e:
            print(e)
        create_scan_activity(scan_history, "Scan aborted", 0)
        messageData = {'status': 'true'}
        messages.add_message(
            request,
            messages.INFO,
            'Scan successfully stopped!')
    else:
        messageData = {'status': 'false'}
        messages.add_message(
            request,
            messages.INFO,
            'Oops! something went wrong!')
    return JsonResponse(messageData)


def schedule_scan(request, host_id):
    domain = Domain.objects.get(id=host_id)
    if request.method == "POST":
        # get engine type
        engine_type = int(request.POST['scan_mode'])
        engine_object = get_object_or_404(EngineType, id=engine_type)
        task_name = engine_object.engine_name + ' for ' + \
            domain.domain_name + \
            ':' + \
            str(datetime.strftime(timezone.now(), '%Y_%m_%d_%H_%M_%S'))
        if request.POST['scheduled_mode'] == 'periodic':
            # periodic task
            frequency_value = int(request.POST['frequency'])
            frequency_type = request.POST['frequency_type']
            if frequency_type == 'minutes':
                period = IntervalSchedule.MINUTES
            elif frequency_type == 'hours':
                period = IntervalSchedule.HOURS
            elif frequency_type == 'days':
                period = IntervalSchedule.DAYS
            elif frequency_type == 'weeks':
                period = IntervalSchedule.DAYS
                frequency_value *= 7
            elif frequency_type == 'months':
                period = IntervalSchedule.DAYS
                frequency_value *= 30

            schedule, created = IntervalSchedule.objects.get_or_create(
                every=frequency_value,
                period=period,)
            PeriodicTask.objects.create(interval=schedule,
                                        name=task_name,
                                        task='reNgine.tasks.initiate_scan',
                                        args=[host_id, 0, 1, engine_type])
        elif request.POST['scheduled_mode'] == 'clocked':
            # clocked task
            schedule_time = request.POST['scheduled_time']
            clock, created = ClockedSchedule.objects.get_or_create(
                clocked_time=schedule_time,)
            PeriodicTask.objects.create(clocked=clock,
                                        one_off=True,
                                        name=task_name,
                                        task='reNgine.tasks.initiate_scan',
                                        args=[host_id, 0, 1, engine_type])
        messages.add_message(
            request,
            messages.INFO,
            'Scan Scheduled for ' +
            domain.domain_name)
        return HttpResponseRedirect(reverse('scheduled_scan_view'))
    engine = EngineType.objects
    custom_engine_count = EngineType.objects.filter(
        default_engine=False).count()
    context = {
        'scan_history_active': 'true',
        'domain': domain,
        'engines': engine,
        'custom_engine_count': custom_engine_count}
    return render(request, 'startScan/schedule_scan_ui.html', context)


def scheduled_scan_view(request):
    scheduled_tasks = PeriodicTask.objects.all().exclude(name='celery.backend_cleanup')
    context = {
        'scheduled_scan_active': 'true',
        'scheduled_tasks': scheduled_tasks,
    }
    return render(request, 'startScan/schedule_scan_list.html', context)


def delete_scheduled_task(request, id):
    task_object = get_object_or_404(PeriodicTask, id=id)
    if request.method == "POST":
        task_object.delete()
        messageData = {'status': 'true'}
        messages.add_message(
            request,
            messages.INFO,
            'Scheduled Scan successfully deleted!')
    else:
        messageData = {'status': 'false'}
        messages.add_message(
            request,
            messages.INFO,
            'Oops! something went wrong!')
    return JsonResponse(messageData)


def change_scheduled_task_status(request, id):
    if request.method == 'POST':
        task = PeriodicTask.objects.get(id=id)
        task.enabled = not task.enabled
        task.save()
    return HttpResponse('')


def change_vuln_status(request, id):
    if request.method == 'POST':
        vuln = Vulnerability.objects.get(id=id)
        vuln.open_status = not vuln.open_status
        vuln.save()
    return HttpResponse('')


def change_subdomain_status(request, id):
    if request.method == 'POST':
        name = Subdomain.objects.get(id=id)
        name.checked = not name.checked
        name.save()
    return HttpResponse('')


def create_scan_object(host_id, engine_type):
    '''
    create task with pending status so that celery task will execute when
    threads are free
    '''
    # get current time
    current_scan_time = timezone.now()
    # fetch engine and domain object
    engine_object = EngineType.objects.get(pk=engine_type)
    domain = Domain.objects.get(pk=host_id)
    task = ScanHistory()
    task.scan_status = -1
    task.domain_name = domain
    task.scan_type = engine_object
    task.start_scan_date = current_scan_time
    task.save()
    # save last scan date for domain model
    domain.start_scan_date = current_scan_time
    domain.save()
    return task.id
