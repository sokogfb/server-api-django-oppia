# oppia/uploader.py

import codecs
import json
import os
import shutil
from zipfile import ZipFile, BadZipfile

import xml.etree.ElementTree as ET

from django.conf import settings
from django.contrib import messages
from django.utils import timezone
from django.utils.translation import ugettext as _

from gamification.models import CourseGamificationEvent, ActivityGamificationEvent, MediaGamificationEvent
from gamification.xml_writer import GamificationXMLWriter
from oppia.models import Course, Section, Activity, Media
from oppia.utils.courseFile import unescape_xml
from quiz.models import Quiz, Question, QuizQuestion, Response, ResponseProps, QuestionProps, QuizProps


def handle_uploaded_file(f, extract_path, request, user):
    zipfilepath = os.path.join(settings.COURSE_UPLOAD_DIR, f.name)

    with open(zipfilepath, 'wb+') as destination:
        for chunk in f.chunks():
            destination.write(chunk)

    try:
        zip_file = ZipFile(zipfilepath)
        zip_file.extractall(path=extract_path)
    except BadZipfile:
        messages.error(request, _("Invalid zip file"), extra_tags="danger")
        return False, 500

    mod_name = ''
    for dir in os.listdir(extract_path)[:1]:
        mod_name = dir

    # check there is at least a sub dir
    if mod_name == '':
        messages.info(request, _("Invalid course zip file"), extra_tags="danger")
        return False, 400

    response = 200
    try:
        course, response = process_course(extract_path, f, mod_name, request, user)
    except Exception as e:
        messages.error(request, e, extra_tags="danger")
        return False, 500
    finally:
        # remove the temp upload files
        shutil.rmtree(extract_path, ignore_errors=True)

    return course, response


def process_course(extract_path, f, mod_name, request, user):
    xml_path = os.path.join(extract_path, mod_name, "module.xml")
    # check that the module.xml file exists
    if not os.path.isfile(xml_path):
        messages.info(request, _("Zip file does not contain a module.xml file"), extra_tags="danger")
        return False, 400

    # parse the module.xml file
    doc = ET.parse(xml_path)
    meta_info = parse_course_meta(doc)

    new_course = False
    oldsections = []
    old_course_filename = None

    # Find if course already exists
    try:
        course = Course.objects.get(shortname=meta_info['shortname'])
        # check that the current user is allowed to wipe out the other course
        if course.user != user:
            messages.info(request, _("Sorry, only the original owner may update this course"))
            return False, 401
        # check if course version is older
        if course.version > meta_info['versionid']:
            messages.info(request, _("A newer version of this course already exists"))
            return False, 400

        # obtain the old sections
        oldsections = list(Section.objects.filter(course=course).values_list('pk', flat=True))
        # wipe out old media
        oldmedia = Media.objects.filter(course=course)
        oldmedia.delete()

        old_course_filename = course.filename
        course.lastupdated_date = timezone.now()

    except Course.DoesNotExist:
        course = Course()
        course.is_draft = True
        new_course = True

    course.shortname = meta_info['shortname']
    course.title = meta_info['title']
    course.description = meta_info['description']
    course.version = meta_info['versionid']
    course.user = user
    course.filename = f.name
    course.save()

    # save gamification events
    if 'gamification' in meta_info:
        events = parse_gamification_events(meta_info['gamification'])
        for event in events:
            # Only add events if the didn't exist previously
            e, created = CourseGamificationEvent.objects.get_or_create(
                course=course, event=event['name'],
                defaults={'points': event['points'], 'user': user})

            if created:
                messages.info(request, _('Gamification for "%(event)s" at course level added') % {'event': e.event})


    process_quizzes_locally = False
    if 'exportversion' in meta_info and meta_info['exportversion'] >= settings.OPPIA_EXPORT_LOCAL_MINVERSION:
        process_quizzes_locally = True

    parse_course_contents(request, doc, course, user, new_course, process_quizzes_locally)
    clean_old_course(request, oldsections, old_course_filename, course)

    tmp_path = replace_zip_contents(xml_path, doc, mod_name, extract_path)
    # Extract the final file into the courses area for preview
    zipfilepath = os.path.join(settings.COURSE_UPLOAD_DIR, f.name)
    shutil.copy(tmp_path + ".zip", zipfilepath)

    course_preview_path = settings.MEDIA_ROOT + "courses/"
    ZipFile(zipfilepath).extractall(path=course_preview_path)

    writer = GamificationXMLWriter(course)
    writer.update_gamification(request.user)

    return course, 200


def parse_course_contents(req, xml_doc, course, user, new_course, process_quizzes_locally):
    # add in any baseline activities
    for meta in xml_doc.findall('meta')[:1]:
        activities = meta.findall("activity")
        if len(activities) > 0:
            section = Section(
                course=course,
                title='{"en": "Baseline"}',
                order=0
            )
            section.save()
            for act in activities:
                parse_and_save_activity(req, user, section, act, new_course, process_quizzes_locally, is_baseline=True)

    # add all the sections and activities
    structure = xml_doc.find("structure")
    if len(structure.findall("section")) == 0:
        course.delete()
        messages.info(req, _("There don't appear to be any activities in this upload file."))
        return

    for idx, s in enumerate(structure.findall("section")):

        activities = s.find('activities')
        # Check if the section contains any activity (to avoid saving an empty one)
        if activities is None or len(activities.findall('activity')) == 0:
            messages.info(req, _("Section ") + str(idx + 1) + _(" does not contain any activities."))
            continue

        title = {}
        for t in s.findall('title'):
            title[t.get('lang')] = t.text

        section = Section(
            course=course,
            title=json.dumps(title),
            order=s.get('order')
        )
        section.save()

        for act in activities.findall("activity"):
            parse_and_save_activity(req, user, section, act, new_course, process_quizzes_locally)


    media_element = xml_doc.find('media')
    if media_element is not None:
        for file_element in media_element.findall('file'):
            media = Media()
            media.course = course
            media.filename = file_element.get("filename")
            url = file_element.get("download_url")
            media.digest = file_element.get("digest")

            if len(url) > Media.URL_MAX_LENGTH:
                messages.info(req, _('File %(filename)s has a download URL larger than the maximum length permitted. The media file has not been registered, so it won\'t be tracked. Please, fix this issue and upload the course again.') % {'filename': media.filename})
            else:
                media.download_url = url
                # get any optional attributes
                for attr_name, attr_value in file_element.attrib.items():
                    if attr_name == "length":
                        media.media_length = attr_value
                    if attr_name == "filesize":
                        media.filesize = attr_value

                media.save()
                # save gamification events
                gamification = file_element.find('gamification')
                events = parse_gamification_events(gamification)

                for event in events:
                    # Only add events if the didn't exist previously
                    e, created = MediaGamificationEvent.objects.get_or_create(
                        media=media, event=event['name'],
                        defaults={'points': event['points'], 'user': req.user})

                    if created:
                        messages.info(req, _('Gamification for "%(event)s" at course level added') % {
                            'event': e.event})



def parse_and_save_activity(req, user, section, act, new_course, process_quiz_locally, is_baseline=False):
    """
    Parses an Activity XML and saves it to the DB
    :param section: section the activity belongs to
    :param act: a XML DOM element containing a single activity
    :param new_course: boolean indicating if it is a new course or existed previously
    :param process_quiz_locally: should the quiz be created based on the JSON contents?
    :param is_baseline: is the activity part of the baseline?
    :return: None
    """

    title = {}
    for t in act.findall('title'):
        title[t.get('lang')] = t.text
    title = json.dumps(title) if title else None

    description = {}
    for t in act.findall('description'):
        description[t.get('lang')] = t.text
    description = json.dumps(description) if description else None

    content = ""
    act_type = act.get("type")
    if act_type == "page" or act_type == "url":
        temp_content = {}
        for t in act.findall("location"):
            if t.text:
                temp_content[t.get('lang')] = t.text
        content = json.dumps(temp_content)
    elif act_type == "quiz" or act_type == "feedback":
        for c in act.findall("content"):
            content = c.text
    elif act_type == "resource":
        for c in act.findall("location"):
            content = c.text
    else:
        content = None

    image = None
    for i in act.findall("image"):
        image = i.get('filename')

    digest = act.get("digest")
    existed = False
    try:
        activity = Activity.objects.get(digest=digest)
        existed = True
    except Activity.DoesNotExist:
        activity = Activity()

    activity.section = section
    activity.title = title
    activity.type = act_type
    activity.order = act.get("order")
    activity.digest = digest
    activity.baseline = is_baseline
    activity.image = image
    activity.content = content
    activity.description = description

    if not existed and not new_course:
        messages.warning(req, _('Activity "%(act)s"(%(digest)s) did not exist previously.') % {'act': activity, 'digest': activity.digest})
    '''
    If we also want to show the activities that previously existed, uncomment this block
    else:
        messages.info(req, _('Activity "%(act)s"(%(digest)s) previously existed. Updated with new information') % {'act': activity, 'digest':activity.digest})
    '''

    if (act_type == "quiz") and process_quiz_locally:
        updated_json = parse_and_save_quiz(req, user, activity, act)
        # we need to update the JSON contents both in the XML and in the activity data
        act.find("content")[0].text = updated_json
        activity.content = updated_json

    activity.save()

    # save gamification events
    gamification = act.find('gamification')
    events = parse_gamification_events(gamification)
    for event in events:
        e, created = ActivityGamificationEvent.objects.get_or_create(
            activity=activity, event=event['name'],
            defaults={'points': event['points'], 'user': req.user})

        if created:
            messages.info(req, _('Gamification for "%(event)s" at activity "%(act)s" added') % {
                'event': e.event, 'act':activity})



def parse_and_save_quiz(req, user, activity, act_xml):
    """
    Parses an Activity XML that is a Quiz and saves it to the DB
    :parm user: the user that uploaded the course
    :param activity: a XML DOM element containing the activity
    :return: None
    """

    quiz_obj = json.loads(activity.content)

    quiz_existed = False
    # first of all, we find the quiz digest to see if it is already saved
    if quiz_obj['props']['digest']:
        quiz_digest = quiz_obj['props']['digest']

        try:
            quizzes = Quiz.objects.filter(quizprops__value=quiz_digest, quizprops__name="digest").order_by('-id')
            quiz_existed = len(quizzes) > 0
            # remove any possible duplicate (possible scenario when transitioning between export versions)
            for quiz in quizzes[1:]:
                quiz.delete()

        except Quiz.DoesNotExist:
            quiz_existed = False

    if quiz_existed:
        try:
            quiz_act = Activity.objects.get(digest=quiz_digest)
            updated_content = quiz_act.content
        except Activity.DoesNotExist:
            updated_content = create_quiz(req, user, quiz_obj, act_xml, activity)
    else:
        updated_content = create_quiz(req, user, quiz_obj, act_xml, activity)

    return updated_content


def create_quiz(req, user, quiz_obj, act_xml, activity=None):

    quiz = Quiz()
    quiz.owner = user
    quiz.title = quiz_obj['title']
    quiz.description = quiz_obj['description']
    quiz.save()

    quiz_obj['id'] = quiz.pk

    # add quiz props
    create_quiz_props(quiz, quiz_obj)

    # add quiz questions
    create_quiz_questions(user, quiz, quiz_obj)

    return json.dumps(quiz_obj)


def get_content(elem, nodeName):
    for node in elem.findall(nodeName):
        return None if node is None else node.text
    return None

def parse_course_meta(xml_doc):

    meta_info = {'versionid': 0, 'shortname': ''}
    for meta in xml_doc.findall('meta')[:1]:
        meta_info['versionid'] = int(meta.find('versionid').text)

        title = {}
        for t in meta.findall('title'):
            title[t.get('lang')] = t.text
        meta_info['title'] = json.dumps(title)

        description = {}
        for t in meta.findall('description'):
            description[t.get('lang')] = t.text
        meta_info['description'] = json.dumps(description)

        meta_info['shortname'] = get_content(meta,'shortname')
        exportversion = meta.find('exportversion')
        if exportversion:
            meta_info['exportversion'] = exportversion
        meta_info['gamification'] = meta.find('gamification')

    return meta_info


def parse_gamification_events(element):
    events = []
    if element is not None:
        for e in element.findall("event"):
            event_name = e.get('name')
            points = e.text
            events.append({'name': event_name, 'points': points})
    return events


def replace_zip_contents(xml_path, xml_doc, mod_name, dest, encoding='utf-8'):

    with codecs.open(xml_path, mode="w", encoding=encoding) as fh:
        new_xml = ET.tostring(xml_doc.getroot(), encoding=encoding).decode('utf-8')
        new_xml = unescape_xml(new_xml)
        fh.write("<?xml version='1.0' encoding='%s'?>\n" % encoding)
        fh.write(new_xml)

    tmp_zipfilepath = os.path.join(dest, 'tmp_course')
    shutil.make_archive(tmp_zipfilepath, 'zip', dest, base_dir=mod_name)
    return tmp_zipfilepath


def clean_old_course(req, oldsections, old_course_filename, course):
    for section in oldsections:
        sec = Section.objects.get(pk=section)
        for act in sec.activities():
            messages.info(req, _('Activity "%(act)s"(%(digest)s) is no longer in the course.') % {'act': act, 'digest': act.digest})
        sec.delete()

    if old_course_filename is not None and old_course_filename != course.filename:
        try:
            os.remove( os.path.join(settings.COURSE_UPLOAD_DIR, old_course_filename) )
        except OSError:
            pass
        
# helper functions

def create_quiz_props(quiz, quiz_obj):
    for prop in quiz_obj['props']:
        if prop is not 'id':
            QuizProps(
                quiz=quiz, name=prop,
                value=quiz_obj['props'][prop]
            ).save()

def create_quiz_questions(user, quiz, quiz_obj ):
    for q in quiz_obj['questions']:

        question = Question(owner=user,
                type=q['question']['type'],
                title=q['question']['title'])
        question.save()

        quiz_question = QuizQuestion(quiz=quiz, question=question, order=q['order'])
        quiz_question.save()

        q['id'] = quiz_question.pk
        q['question']['id'] = question.pk

        for prop in q['question']['props']:
            if prop is not 'id':
                QuestionProps(
                    question=question, name=prop,
                    value=q['question']['props'][prop]
                ).save()

        for r in q['question']['responses']:
            response = Response(
                owner=user,
                question=question,
                title=r['title'],
                score=r['score'],
                order=r['order']
            )
            response.save()
            r['id'] = response.pk

            for prop in r['props']:
                if prop is not 'id':
                    ResponseProps(
                        response=response, name=prop,
                        value=r['props'][prop]
                    ).save()
