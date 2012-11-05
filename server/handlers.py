import re
import os
import stat
import urllib
import json
import uuid
import functools
import logging
import hashlib
import time
import datetime
from pprint import pprint

from bson.objectid import ObjectId
import tornado.web
import tornado.gen
import tornado.httpclient
import tornado.curl_httpclient
import tornado.ioloop
from PIL import Image
from tornado_utils.routes import route
from rq import Queue
import motor
from utils import (
    mkdir, make_tile, make_tiles, make_thumbnail, delete_image,
    count_all_tiles
)
from optimizer import optimize_images, optimize_thumbnails
from awsuploader import upload_tiles, upload_original
from resizer import make_resize
from emailer import send_url
import settings


def commafy(s):
    r = []
    for i, c in enumerate(reversed(str(s))):
        if i and (not (i % 3)):
            r.insert(0, ',')
        r.insert(0, c)
    return ''.join(r)


def sample_queue_job():
    # used to check that the queue workers are awake
    return "OK"


class BaseHandler(tornado.web.RequestHandler):

    DEFAULT_RANGE_MIN = 2
    DEFAULT_RANGE_MAX = 5
    DEFAULT_ZOOM = 3
    DEFAULT_EXTENSION = 'png'

    @property
    def redis(self):
        return self.application.redis

    @property
    def db(self):
        return self.application.db

    @property
    def queue(self):
        return self.application.queue

    def get_current_user(self):
        return self.get_secure_cookie('user')

    def render(self, template, **options):
        options['user'] = self.get_current_user()
        options['debug'] = self.application.settings['debug']
        if options['user']:
            options['gravatar_url'] = self._get_gravatar_url(options['user'])
        if 'page_on' not in options:
            page_on = self.request.path.split('/')[-1]
            if not page_on:
                page_on = '/'
            options['page_on'] = page_on
        options['PROJECT_TITLE'] = settings.PROJECT_TITLE
        return super(BaseHandler, self).render(template, **options)

    def _get_gravatar_url(self, email):
        d_url = self.static_url('images/anonymous_32.png')
        if d_url.startswith('//'):
            default = '%s:%s' % (self.request.protocol, d_url)
        else:
            default = '%s://%s%s' % (self.request.protocol,
                                     self.request.host,
                                     d_url)
        # nasty hack so that gravatar can serve a default
        # icon when on a local URL
        default = default.replace('http://tiler/', 'http://hugepic.io/')

        size = 32
        # construct the url
        gravatar_url = (
            "http://www.gravatar.com/avatar/" +
            hashlib.md5(email.lower()).hexdigest() +
            "?" +
            urllib.urlencode({
                'd': default,
                's': str(size)
            })
        )

        return gravatar_url

    def static_url(self, path, **kwargs):
        if self.application.settings['embed_static_url_timestamp']:
            ui_module = self.application.ui_modules['StaticURL'](self)
            try:
                return ui_module.render(path, **kwargs)
            except OSError:
                logging.debug("%r does not exist" % path)
        return super(BaseHandler, self).static_url(path)

    def get_cdn_prefix(self):
        """return something that can be put in front of the static filename
        E.g. if filename is '/static/image.png' and you return
        '//cloudfront.com' then final URL presented in the template becomes
        '//cloudfront.com/static/image.png'
        """
        return self.application.settings.get('cdn_prefix')

    def make_thumbnail_url(self, fileid, width, extension='png',
                           absolute_url=False):
        url = '/thumbnails/%s/%s/%s/%s.%s' % (
            fileid[:1],
            fileid[1:3],
            fileid[3:],
            width,
            extension
        )
        cdn_prefix = self.get_cdn_prefix()
        if cdn_prefix:
            url = cdn_prefix + url
        elif absolute_url:
            url = '%s://%s%s' % (self.request.protocol,
                                 self.request.host,
                                 url)
        return url

    def clear_thumbnail_grid_cache(self):
        cache_keys_key = 'thumbnail_grid:keys'
        for key in self.redis.lrange(cache_keys_key, 0, -1):
            self.redis.delete(key)
        self.redis.delete(cache_keys_key)

    def remember_thumbnail_grid_cache_key(self, key):
        cache_keys_key = 'thumbnail_grid:keys'
        self.redis.lpush(cache_keys_key, key)

    def get_extra_rows_cols(self, zoom):
        if zoom == 2:
            return 0
        return 1  # default

    def make_destination(self, fileid, content_type=None):
        root = os.path.join(
            self.application.settings['static_path'],
            'uploads'
        )
        if not os.path.isdir(root):
            os.mkdir(root)
        destination = os.path.join(
            root,
            fileid[:1],
            fileid[1:3],
        )
        # so far, it's the directory
        mkdir(destination)
        # this is the full file path
        destination += '/%s' % fileid[3:]
        if content_type is None:
            content_type = self.redis.get('contenttype:%s' % fileid)
        # complete it with the extension
        if content_type == 'image/png':
            destination += '.png'
        else:
            assert content_type == 'image/jpeg', content_type
            destination += '.jpg'

        return destination


class ThumbnailGridRendererMixin(object):

    @tornado.gen.engine
    def render_thumbnail_grid(self, search, page, page_size, callback):
        data = {
            'recent_images_rows': [],
        }
        skip = page_size * (page - 1)
        cursor = (
            self.db.images.find(search)
            .sort([('date', -1)])
            .limit(page_size)
            .skip(skip)
        )
        image = yield motor.Op(cursor.next_object)
        row = []
        count = 0
        #_now = datetime.datetime.utcnow()
        while image:
            if image.get('width') and image.get('featured', True):
                row.append(image)

            count += 1
            image = yield motor.Op(cursor.next_object)

            if len(row) == 3:
                data['recent_images_rows'].append(row)
                row = []
        if row:
            data['recent_images_rows'].append(row)

        callback((self.render_string('_thumbnail_grid.html', **data), count))


hits_html_regex = re.compile('<!--hits:(\w+)-->')


@route('/', name='home')
class HomeHandler(BaseHandler, ThumbnailGridRendererMixin):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self):
        data = {
            'yours': False
        }

        search = {}
        if self.get_argument('user', None):
            search['user'] = self.get_argument('user')
            data['yours'] = True
        page = int(self.get_argument('page', 1))

        total_count = yield motor.Op(self.db.images.find(search).count)

        page_size = 12
        _search_values = search.values()
        cache_key = (
            'thumbnail_grid:%s:%s:%s' %
            (page, page_size, _search_values)
        )
        result = self.redis.get(cache_key)
        if result:
            thumbnail_grid, count = tornado.escape.json_decode(result)
        else:
            logging.warning('Thumbnail grid cache miss (%r)' % cache_key)
            thumbnail_grid, count = yield tornado.gen.Task(
                self.render_thumbnail_grid,
                search, page, page_size
            )
            self.redis.setex(
                cache_key,
                tornado.escape.json_encode([thumbnail_grid, count]),
                60 * 60
            )
            self.remember_thumbnail_grid_cache_key(cache_key)
        thumbnail_grid = self.insert_hits_html(thumbnail_grid)
        data['thumbnail_grid'] = thumbnail_grid

        pagination = None
        if total_count > count:
            # pagination in order!
            pagination = {
                'current_page': page,
                'range': range(1, total_count / page_size + 2)
            }
            if (page - 1) * page_size > 0:
                pagination['prev'] = page - 1
            if page * page_size < total_count:
                pagination['next'] = page + 1

        data['pagination'] = pagination
        data['show_hero_unit'] = self.get_argument('page', None) is None
        self.render('index.html', **data)

    def insert_hits_html(self, html):
        _now = datetime.datetime.utcnow()

        def replacer(match):
            fileid = match.groups()[0]
            hit_key = 'hits:%s' % fileid
            hit_month_key = (
                'hits:%s:%s:%s' %
                (_now.year, _now.month, fileid)
            )
            hits = self.redis.get(hit_key)
            hits_this_month = (
                self.redis.get(hit_month_key)
            )
            if hits:
                if hits == 1:
                    h = '1 hit'
                else:
                    h = '%s hits' % commafy(hits)
                if hits_this_month and hits_this_month != hits:
                    if hits_this_month == 1:
                        h += ' (1 hit this month)'
                    else:
                        h += ' (%s hits this month)' % commafy(hits_this_month)
                return h + '<br>'
            return match.group()
        html = hits_html_regex.sub(replacer, html)
        return html


@route('/(\w{9})', 'image')
class ImageHandler(BaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, fileid):
        image_filename = (
            fileid[:1] +
            '/' +
            fileid[1:3] +
            '/' +
            fileid[3:]
        )
        # we might want to read from a database what the most
        # appropriate numbers should be here.
        ranges = [self.DEFAULT_RANGE_MIN, self.DEFAULT_RANGE_MAX]
        default_zoom = self.DEFAULT_ZOOM

        metadata_key = 'metadata:%s' % fileid
        metadata = self.redis.get(metadata_key)
        #metadata=None;self.redis.delete('uploading:%s' % fileid)

        if metadata and 'width' not in metadata:
            # legacy
            metadata = None
        if metadata and 'date_timestamp' not in metadata:
            # legacy
            metadata = None

        if metadata is not None:
            metadata = json.loads(metadata)
            content_type = metadata['content_type']
            owner = metadata['owner']
            title = metadata['title']
            date_timestamp = metadata['date_timestamp']
            width = metadata['width']
            cdn_domain = metadata.get('cdn_domain')

        else:
            logging.info("Meta data cache miss (%s)" % fileid)
            document = yield motor.Op(
                self.db.images.find_one,
                {'fileid': fileid}
            )
            if not document:
                raise tornado.web.HTTPError(404, "File not found")

            content_type = document['contenttype']
            owner = document['user']
            title = document.get('title', '')
            width = document['width']
            cdn_domain = document.get('cdn_domain', None)
            date_timestamp = time.mktime(document['date'].timetuple())

            metadata = {
                'content_type': content_type,
                'owner': owner,
                'title': title,
                'date_timestamp': date_timestamp,
                'width': width,
                'cdn_domain': cdn_domain,
            }
            if document.get('ranges'):
                metadata['ranges'] = document['ranges']
            self.redis.setex(
                metadata_key,
                json.dumps(metadata),
                60 * 60  # * 24
            )

        now = time.mktime(datetime.datetime.utcnow().timetuple())
        age = now - date_timestamp

        if metadata.get('ranges'):
            ranges = metadata.get('ranges')
        else:
            ranges = []
            _range = self.DEFAULT_RANGE_MIN
            while True:
                ranges.append(_range)
                range_width = 256 * (2 ** _range)
                if range_width > width or _range >= self.DEFAULT_RANGE_MAX:
                    break
                _range += 1

        can_edit = self.get_current_user() == owner

        if content_type == 'image/jpeg':
            extension = 'jpg'
        elif content_type == 'image/png':
            extension = 'png'
        else:
            print "Guessing extension :("
            extension = self.DEFAULT_EXTENSION
        extension = self.get_argument('extension', extension)
        assert extension in ('png', 'jpg'), extension

        if age > 60 * 60 and not cdn_domain:
            # it might be time to upload this to S3
            lock_key = 'uploading:%s' % fileid
            if self.redis.get(lock_key):
                print "AWS uploading is locked"
            else:
                # we're ready to upload it
                _no_tiles = count_all_tiles(
                    fileid,
                    self.application.settings['static_path']
                )
                self.redis.setex(lock_key, 1, 60 * 60)
                priority = (
                    self.application.settings['debug'] and 'default' or 'low'
                )
                q = Queue(priority, connection=self.redis)
                logging.info("About to upload %s tiles" % _no_tiles)
                # bulk the queue workers with 100 each
                for i in range(_no_tiles / 100 + 1):
                    q.enqueue(
                        upload_tiles,
                        fileid,
                        self.application.settings['static_path'],
                        max_count=100
                    )

                # upload the original
                q.enqueue(
                    upload_original,
                    fileid,
                    extension,
                    self.application.settings['static_path'],
                    settings.ORIGINALS_BUCKET_ID
                )

        og_image_url = None
        # if the image is old enough to have been given a chance to generate a
        # thumbnail, then set that
        if age > 60:
            og_image_url = self.make_thumbnail_url(
                fileid,
                100,
                extension=extension,
                absolute_url=True,
            )

        self.render(
            'image.html',
            page_title=title or '/%s' % fileid,
            image_filename=image_filename,
            ranges=ranges,
            default_zoom=default_zoom,
            extension=extension,
            can_edit=can_edit,
            age=age,
            og_image_url=og_image_url,
            prefix=cdn_domain and '//' + cdn_domain or '',
        )


@route('/(\w{9})/hit', 'image_hitcounter')
class ImageHitCounterHandler(BaseHandler):

    def post(self, fileid):

        # increment a hit counter
        _now = datetime.datetime.utcnow()
        hit_key = 'hits:%s' % fileid
        hit_month_key = (
            'hits:%s:%s:%s' %
            (_now.year, _now.month, fileid)
        )
        self.redis.incr(hit_key)
        self.redis.incr(hit_month_key)
        self.write('OK')


@route('/(\w{9})/weight', 'image_weight')
class ImageWeightCounterHandler(BaseHandler):

    def check_xsrf_cookie(self):
        pass

    def post(self, fileid):
        urls = self.get_argument('urls')
        extension = self.get_argument('extension')
        root = os.path.join(
            self.settings['static_path'],
            'tiles',
            fileid[:1],
            fileid[1:3],
            fileid[3:],
            '256',
        )
        bytes = 0
        for each in urls.split('|'):
            path = os.path.join(root, each + extension)
            try:
                bytes += os.stat(path)[stat.ST_SIZE]
            except OSError:
                pass
        self.write({'bytes': bytes})


@route('/(\w{9})/metadata', 'image_metadata')
class ImageMetadataHandler(BaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, fileid):
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        data = {
            'title': document.get('title'),
            'description': document.get('description'),
        }
        self.write(data)
        self.finish()


@route('/(\w{9})/edit', 'image_edit')
class ImageEditHandler(BaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, fileid):
        current_user = self.get_current_user()
        if not current_user:
            raise tornado.web.HTTPError(403, "Not logged in")

        title = self.get_argument('title', u'')
        description = self.get_argument('description', u'')
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if document['user'] != current_user:
            raise tornado.web.HTTPError(403, "Not yours to edit")

        data = {
            'title': title,
            'description': description
        }
        yield motor.Op(
            self.db.images.update,
            {'_id': document['_id']},
            {'$set': data}
        )

        metadata_key = 'metadata:%s' % fileid
        self.redis.delete(metadata_key)

        self.write(data)
        self.finish()


class AnnotationBaseHandler(BaseHandler):

    def get_annotation_html(self, annotation, yours):
        html = (
            '<p><strong>%s</strong></p>' %
            tornado.escape.linkify(annotation['title'])
        )
        if yours:
            html += (
                '<p><a href="#" onclick="return Annotations.edit(\'%s\')"'
                '>edit</a> &ndash; '
                '<a href="#" onclick="return Annotations.delete_(\'%s\')"'
                '>delete</a></p>' %
                (annotation['_id'], annotation['_id'])
            )
        return html


@route('/(\w{9})/annotations', 'image_annotations')
class ImageAnnotationsHandler(AnnotationBaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, fileid):
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if not document:
            raise tornado.web.HTTPError(404, "Not found")
        cursor = self.db.annotations.find({'image': document['_id']})
        annotation = yield motor.Op(cursor.next_object)
        annotations = []
        current_user = self.get_current_user()

        while annotation:
            yours = annotation['user'] == current_user
            data = {
                'id': str(annotation['_id']),
                'title': annotation['title'],
                'html': self.get_annotation_html(annotation, yours),
                'type': annotation['type'],
                'latlngs': annotation['latlngs'],
                'yours': yours,
            }
            if annotation.get('radius'):
                assert data['type'] == 'circle'
                data['radius'] = annotation['radius']

            annotations.append(data)
            annotation = yield motor.Op(cursor.next_object)

        data = {'annotations': annotations}
        self.write(data)
        self.finish()

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, fileid):
        current_user = self.get_current_user()
        if not current_user:
            raise tornado.web.HTTPError(403, "Not logged in")
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if not document:
            raise tornado.web.HTTPError(404, "Not found")
        #if document['user'] != current_user:
        #    raise tornado.web.HTTPError(403, "Not yours to annotate")

        title = self.get_argument('title').strip()
        type_ = self.get_argument('type')
        _recognized_types = (
            'polyline',
            'rectangle',
            'polygon',
            'marker',
            'circle',
        )
        assert type_ in _recognized_types, type_
        latlngs_json = self.get_argument('latlngs')
        latlngs = tornado.escape.json_decode(latlngs_json)
        #pprint(latlngs)
        # example rectangle:
        # {u'_northEast': {u'lat': -47.1598400130443, u'lng': 81.5625},
        #  u'_southWest': {u'lat': -58.26328705248601, u'lng': 24.43359375}}
        if type_ == 'rectangle':
            # because rectangles used bounds instead
            latlngs = [latlngs['_southWest'], latlngs['_northEast']]
        if type_ == 'circle' or type_ == 'marker':
            latlngs = [latlngs]
        latlngs = [[x['lat'], x['lng']] for x in latlngs]

        options = {}
        if self.get_argument('options', None):
            options.update(
                tornado.escape.json_decode(self.get_argument('options'))
            )

        annotation = {
            'image': document['_id'],
            'latlngs': latlngs,
            'type': type_,
            'title': title,
            'user': current_user,
            'date': datetime.datetime.utcnow(),
            'approved': document['user'] == current_user,
            'options': options,
        }
        if type_ == 'circle':
            annotation['radius'] = float(self.get_argument('radius'))

        _id = yield motor.Op(
            self.db.annotations.insert,
            annotation,
            safe=False
        )
        annotation['_id'] = _id

        data = {
            'html': self.get_annotation_html(annotation, True),
            'id': str(_id),
            'title': title,
        }
        self.write(data)
        self.finish()


@route('/(\w{9})/annotations/move', 'image_annotations_move')
class ImageAnnotationsMoveHandler(AnnotationBaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, fileid):
        current_user = self.get_current_user()
        if not current_user:
            raise tornado.web.HTTPError(403, "Not logged in")
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if not document:
            raise tornado.web.HTTPError(404, "Not found")

        annotation = yield motor.Op(
            self.db.annotations.find_one,
            {'_id': ObjectId(self.get_argument('id'))}
        )
        if not annotation:
            raise tornado.web.HTTPError(404, "Marker not found")
        if annotation['user'] != current_user:
            raise tornado.web.HTTPError(403, "Not yours to annotate")

        lat = round(float(self.get_argument('lat')), 3)
        lng = round(float(self.get_argument('lng')), 3)
        data = {
            'latlngs': [[lat, lng]]
        }
        yield motor.Op(
            self.db.annotations.update,
            {'_id': annotation['_id']},
            {'$set': data}
        )

        self.write({'lat': lat, 'lng': lng})
        self.finish()


@route('/(\w{9})/annotations/edit', 'image_annotations_edit')
class ImageAnnotationsEditHandler(AnnotationBaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, fileid):
        current_user = self.get_current_user()
        if not current_user:
            raise tornado.web.HTTPError(403, "Not logged in")
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if not document:
            raise tornado.web.HTTPError(404, "Not found")

        annotation = yield motor.Op(
            self.db.annotations.find_one,
            {'_id': ObjectId(self.get_argument('id'))}
        )
        if not annotation:
            raise tornado.web.HTTPError(404, "annotation not found")
        if annotation['user'] != current_user:
            raise tornado.web.HTTPError(403, "Not yours to annotate")

        title = self.get_argument('title').strip()
        data = {
            'title': title
        }
        yield motor.Op(
            self.db.annotations.update,
            {'_id': annotation['_id']},
            {'$set': data}
        )
        annotation['title'] = title

        yours = annotation['user'] == current_user
        html = self.get_annotation_html(annotation, yours)
        self.write({'html': html, 'title': title})
        self.finish()


@route('/(\w{9})/annotations/delete', 'image_annotations_delete')
class ImageAnnotationsDeleteHandler(AnnotationBaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, fileid):
        current_user = self.get_current_user()
        if not current_user:
            raise tornado.web.HTTPError(403, "Not logged in")
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if not document:
            raise tornado.web.HTTPError(404, "Not found")

        marker = yield motor.Op(
            self.db.annotations.find_one,
            {'_id': ObjectId(self.get_argument('id'))}
        )
        if not marker:
            raise tornado.web.HTTPError(404, "Marker not found")
        if marker['user'] != current_user:
            raise tornado.web.HTTPError(403, "Not yours")

        yield motor.Op(
            self.db.annotations.remove,
            {'_id': marker['_id']}
        )
        self.write('OK')
        self.finish()


@route('/(\w{9})/delete', 'image_delete')
class ImageDeleteHandler(BaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self, fileid):
        current_user = self.get_current_user()
        if not current_user:
            raise tornado.web.HTTPError(403, "Not logged in")
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        if document['user'] != current_user:
            raise tornado.web.HTTPError(403, "Not yours to edit")

        yield motor.Op(
            self.db.images.remove,
            {'_id': document['_id']}
        )
        metadata_key = 'metadata:%s' % fileid
        self.redis.delete(metadata_key)

        priority = self.application.settings['debug'] and 'default' or 'low'
        q = Queue(priority, connection=self.redis)
        image_split = (
            fileid[:1] +
            '/' +
            fileid[1:3] +
            '/' +
            fileid[3:]
        )
        q.enqueue(
            delete_image,
            image_split,
            self.application.settings['static_path']
        )

        try:
            self.clear_thumbnail_grid_cache()
        except:
            logging.error('Unable to clear_thumbnail_grid_cache()',
                          exc_info=True)

        self.write("Deleted")
        self.finish()


@route('/upload', 'upload')
class UploadHandler(BaseHandler):

    def get(self):
        self.render('upload.html')


@route('/upload/preview', 'upload_preview')
class PreviewUploadHandler(UploadHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self):
        url = self.get_argument('url')
        if not self.get_current_user():
            raise tornado.web.HTTPError(403, "You must be logged in")
        http_client = tornado.httpclient.AsyncHTTPClient()
        head_response = yield tornado.gen.Task(
            http_client.fetch,
            url,
            method='HEAD'
        )

        if head_response.code == 599:
            message = (
                'Fetching the image timed out. '
                'Perhaps try again a little later.'
            )
            self.write({'error': message})
            self.finish()
            return

        if not head_response.code == 200:
            self.write({'error': head_response.body})
            self.finish()
            return

        content_type = head_response.headers['Content-Type']
        if content_type not in ('image/jpeg', 'image/png'):
            if ((url.lower().endswith('.jpg') or url.lower().endswith('.png'))
                and head_response.headers.get('Content-Length')):
                logging.warning("Possibly not an image")

                if url.lower().endswith('.jpg'):
                    content_type = 'image/jpeg'
                elif url.lower().endswith('.png'):
                    content_type = 'image/png'
                else:
                    content_type = 'unknown'
            else:
                if content_type == 'text/html':
                    self.write({'error': "URL not an image. It's a web page"})
                    self.finish()
                    return
                raise tornado.web.HTTPError(
                    400,
                    "Unrecognized content type '%s'" % content_type
                )
        try:
            expected_size = int(head_response.headers['Content-Length'])
            if expected_size == 1:
                # e.g. raw.github.com does this
                raise KeyError
        except KeyError:
            # sometimes images don't have a Content-Length but still work
            logging.warning("No Content-Length (content-encoding:%r)" %
                            head_response.headers.get('Content-Encoding', ''))
            expected_size = 0

        fileid = uuid.uuid4().hex[:9]
        _count = yield motor.Op(self.db.images.find({'fileid': fileid}).count)
        while _count:
            fileid = uuid.uuid4().hex[:9]
            _count = yield motor.Op(
                self.db.images.find({'fileid': fileid}).count
            )

        document = {
            'fileid': fileid,
            'source': url,
            'date': datetime.datetime.utcnow(),
            'user': self.get_current_user()
        }
        self.redis.setex(
            'contenttype:%s' % fileid,
            content_type,
            60 * 60
        )
        document['contenttype'] = content_type
        self.redis.setex(
            'expectedsize:%s' % fileid,
            expected_size,
            60 * 60
        )
        if expected_size:
            document['size'] = expected_size
        yield motor.Op(self.db.images.insert, document, safe=False)

        self.write({
            'fileid': fileid,
            'content_type': content_type,
            'expected_size': expected_size,
        })
        self.finish()


@route('/upload/progress', 'upload_progress')
class ProgressUploadHandler(UploadHandler):

    def get(self):
        if not self.get_current_user():
            raise tornado.web.HTTPError(403, "You must be logged in")
        fileid = self.get_argument('fileid')
        destination = self.make_destination(fileid)
        data = {
            'done': 0
        }

        if os.path.isfile(destination):
            size = os.stat(destination)[stat.ST_SIZE]
            data['done'] = size
        self.write(data)


def my_streaming_callback(destination_file, data):
    destination_file.write(data)


class TileMakerMixin(object):

    @tornado.gen.engine
    def prepare_all_tiles(self, fileid, destination, ranges, extension,
                          callback):
        had_to_give_up = False
        image_split = fileid[:1] + '/' + fileid[1:3] + '/' + fileid[3:]

        if self.application.settings['debug']:
            q_high = Queue('default', connection=self.redis)
            q_default = Queue('default', connection=self.redis)
            q_low = Queue('default', connection=self.redis)
        else:
            q_high = Queue('high', connection=self.redis)
            q_default = Queue('default', connection=self.redis)
            q_low = Queue('default', connection=self.redis)

        resize_jobs = {}
        for zoom in ranges:
            resize_jobs[zoom] = q_high.enqueue(
                make_resize,
                destination,
                zoom
            )

        for zoom in ranges:
            print "Resizes:"
            pprint(dict((k, v.result) for (k,v) in resize_jobs.items()))
            # we can't add this job until the resize job is complete
            ioloop_instance = tornado.ioloop.IOLoop.instance()
            delay = 0.5
            max_total_delay = 10
            total_delay = 0
            while True:
                print "\tsleeping for", delay, "seconds",
                print "(%s total delay)" % total_delay
                yield tornado.gen.Task(
                    ioloop_instance.add_timeout,
                    time.time() + delay
                )
                delay += 0.1
                total_delay += delay
                if resize_jobs[zoom].result is not None:
                    del resize_jobs[zoom]
                    break
                # The maximum time the AJAX post will wait is about
                # 60 seconds. So we don't want to max out the
                # total delay time possible.
                if total_delay > max_total_delay:
                    logging.warning(
                        "Had to give up on %d for %s" %
                        (zoom, fileid)
                    )
                    had_to_give_up = True
                    break

            #if resize_jobs.get(zoom):
            #    # it's still going, we're going to have to do this
            #    # some other time
            #    continue

            width = 256 * (2 ** zoom)
            extra = self.get_extra_rows_cols(zoom)
            # the reason for the `extra` is because some tiles
            # are going *outside* the original width and height
            # of the original
            # We increment the extra based on the width
            #print "ZOOM", zoom
            #print "\tWIDTH", width
            #print "\tEXTRA", extra
            #print "\tDIVISION", (width / 256)
            cols = rows = extra + width / 256
            q_default.enqueue(
                make_tiles,
                image_split,
                256,
                zoom,
                rows,
                cols,
                extension,
                self.application.settings['static_path']
            )

        # it's important to know how the thumbnail needs to be generated
        # and it's important to do the thumbnail soon since otherwise
        # it might severly delay the home page where the thumbnail is shown
        q_high.enqueue(
            make_thumbnail,
            image_split,
            100,
            extension,
            self.application.settings['static_path']
        )

        # pause for 2 seconds just to be sure enough images have been
        # created before we start optimizing
        ioloop_instance = tornado.ioloop.IOLoop.instance()
        yield tornado.gen.Task(
            ioloop_instance.add_timeout,
            time.time() + 2
        )

        # once that's queued up we can start optimizing
        for zoom in ranges:
            q_low.enqueue(
                optimize_images,
                image_split,
                zoom,
                extension,
                self.application.settings['static_path']
            )

        # lastly, optimize the thumbnail too
        q_low.enqueue(
            optimize_thumbnails,
            image_split,
            extension,
            self.application.settings['static_path']
        )

        callback(had_to_give_up)

    @tornado.gen.engine
    def email_about_upload(self, fileid, email, callback):
        base_url = (
            '%s://%s' %
            (self.request.protocol, self.request.host)
        )
        url = base_url + self.reverse_url('image', fileid)

        q = Queue('default', connection=self.redis)
        q.enqueue(
            send_url,
            url,
            fileid,
            email,
            self.application.settings['debug']
        )
        callback()


@route('/upload/download', 'upload_download')
class DownloadUploadHandler(UploadHandler, TileMakerMixin):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self):
        if not self.get_current_user():
            raise tornado.web.HTTPError(403, "You must be logged in")
        fileid = self.get_argument('fileid')
        document = yield motor.Op(
            self.db.images.find_one,
            {'fileid': fileid}
        )
        url = document['source']
        tornado.httpclient.AsyncHTTPClient.configure(
            tornado.curl_httpclient.CurlAsyncHTTPClient
        )
        http_client = tornado.httpclient.AsyncHTTPClient()
        destination = self.make_destination(fileid)
        destination_file = open(destination, 'wb')
        response = yield tornado.gen.Task(
            http_client.fetch,
            url,
            headers={},
            request_timeout=600.0,  # 20.0 is the default
            streaming_callback=functools.partial(my_streaming_callback,
                                                 destination_file)
        )
        destination_file.close()
        if response.code == 200:
            size = Image.open(destination).size
            if size[0] < 256 * (2 ** self.DEFAULT_RANGE_MIN):
                self.write({
                    'error': 'Picture too small (%sx%s)' % size
                })

                # reverse the upload by deleting the record
                yield motor.Op(
                    self.db.images.remove,
                    {'_id': document['_id']}
                )
                os.remove(destination)
                self.finish()
                return

            data = {'width': size[0], 'height': size[1]}
            if not document.get('size'):
                data['size'] = os.stat(destination)[stat.ST_SIZE]
            yield motor.Op(
                self.db.images.update,
                {'_id': document['_id']},
                {'$set': data}
            )
            area = size[0] * size[1]
            r = 1.0 * size[0] / size[1]

            ranges = []
            _range = self.DEFAULT_RANGE_MIN
            while True:
                ranges.append(_range)
                range_width = 256 * (2 ** _range)
                range_height = range_width / r
                range_area = range_width * range_height
                if _range >= self.DEFAULT_RANGE_MAX:
                    break
                if range_area > area:
                    break
                _range += 1

            # since zoom level 3 is the default, make sure that's
            # prepared first
            ranges.remove(self.DEFAULT_ZOOM)
            ranges.insert(0, self.DEFAULT_ZOOM)
            extension = destination.split('.')[-1]

            #tiles_made = yield tornado.gen.Task(
            had_to_give_up = yield tornado.gen.Task(
                self.prepare_all_tiles,
                fileid,
                destination,
                ranges,
                extension
            )
            # clear the home page cache
            try:
                self.clear_thumbnail_grid_cache()
            except:
                logging.error('Unable to clear_thumbnail_grid_cache()',
                              exc_info=True)
            if had_to_give_up:
                logging.warning(
                    "Had to give up when generating tiles %r"
                    % fileid
                )
                self.write({
                    'email': document['user']
                })
            else:
                self.write({
                    'url': self.reverse_url('image', fileid),
                })

            yield tornado.gen.Task(
                self.email_about_upload,
                fileid,
                document['user']
            )
        else:
            try:
                os.remove(destination)
            except:
                logging.error("Unable to remove %s" % destination,
                              exc_info=True)
            self.write({
                'error': "FAILED TO DOWNLOAD\n%s\n%s\n" %
                         (response.code, response.body)
            })
        self.finish()


@route('/auth/signout/', 'signout')
class SignoutHandler(BaseHandler):
    def get(self):
        self.write("Must use POST")

    def post(self):
        self.clear_cookie('user')
        self.redirect('/')


@route('/auth/browserid/', 'browserid')
class BrowserIDAuthLoginHandler(BaseHandler):

    def check_xsrf_cookie(self):
        pass

    @tornado.web.asynchronous
    @tornado.gen.engine
    def post(self):
        assertion = self.get_argument('assertion')
        http_client = tornado.httpclient.AsyncHTTPClient()
        url = 'https://verifier.login.persona.org/verify'
        if self.request.host != settings.BROWSERID_DOMAIN:
            logging.warning(
                "%r != %r" %
                (self.request.host, settings.BROWSERID_DOMAIN)
            )

        data = {
            'assertion': assertion,
            'audience': settings.BROWSERID_DOMAIN,
        }
        response = yield tornado.gen.Task(
            http_client.fetch,
            url,
            method='POST',
            body=urllib.urlencode(data),
        )
        if 'email' in response.body:
            # all is well
            struct = tornado.escape.json_decode(response.body)
            assert struct['email']
            email = struct['email']
            self.set_secure_cookie('user', email, expires_days=90)
        else:
            struct = {'error': 'Email could not be verified'}
        self.write(struct)
        self.finish()


@route(r'/tiles/(?P<image>\w{1}/\w{2}/\w{6})/(?P<size>\d+)'
       r'/(?P<zoom>\d+)/(?P<row>\d+),(?P<col>\d+)'
       r'.(?P<extension>jpg|png)',
       name='tile')
class TileHandler(BaseHandler):
    """Tiles are supposed to be created with a queue. This handler is a
    fallback for when tiles weren't created by queue.
    So if this is called and needed perhaps not all tiles were uploaded
    to S3.
    """

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, image, size, zoom, row, col, extension):
        if extension == 'png':
            self.set_header('Content-Type', 'image/png')
        else:
            self.set_header('Content-Type', 'image/jpeg')
        size = int(size)
        if size != 256:
            raise tornado.web.HTTPError(400, 'size must be 256')

        priority = self.application.settings['debug'] and 'default' or 'high'
        q = Queue(priority, connection=self.redis)
        job = q.enqueue(
            make_tile,
            image,
            size,
            zoom,
            row,
            col,
            extension,
            self.application.settings['static_path']
        )
        ioloop_instance = tornado.ioloop.IOLoop.instance()
        delay = 0.1
        while True:
            yield tornado.gen.Task(
                ioloop_instance.add_timeout,
                time.time() + delay
            )
            delay *= 2
            if job.result is not None:
                save_filepath = job.result
                break

        try:
            _cache_seconds = 60 * 60 * 24
            self.set_header(
                'Cache-Control',
                'max-age=%d, public' % _cache_seconds
            )
            if _cache_seconds > 3600:
                _expires = (
                    datetime.datetime.utcnow() +
                    datetime.timedelta(seconds=_cache_seconds)
                )
                self.set_header(
                    'Expires',
                    _expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
                )
            self.write(open(save_filepath, 'rb').read())
            priority = (
                self.application.settings['debug'] and 'default' or 'low'
            )
            fileid = image.replace('/', '')

            lock_key = 'uploading:%s' % fileid
            if not self.redis.get(lock_key):
                q = Queue(priority, connection=self.redis)
                q.enqueue(
                    upload_tiles,
                    fileid,
                    self.application.settings['static_path'],
                    max_count=10,
                    only_if_no_cdn_domain=True
                )

            # this is used by the admin
            count_key = 'count_all_tiles:%s' % image['fileid']
            self.redis.delete(count_key)

        except IOError:
            self.set_header('Content-Type', 'image/png')
            self.set_header(
                'Cache-Control',
                'max-age=0'
            )
            broken_filepath = os.path.join(
                self.application.settings['static_path'],
                'images',
                'broken.png'
            )
            self.write(open(broken_filepath, 'rb').read())

        self.finish()


@route(r'/thumbnails/(?P<image>\w{1}/\w{2}/\w{6})/(?P<width>\w{1,3})'
       r'.(?P<extension>png|jpg)',
       name='thumbail')
class ThumbnailHandler(BaseHandler):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, image, width, extension):
        width = int(width)
        assert width > 0 and width < 1000, width

        # stick it on a queue
        q = Queue(connection=self.redis)

        job = q.enqueue(
            make_thumbnail,
            image,
            width,
            extension,
            self.application.settings['static_path']
        )
        ioloop_instance = tornado.ioloop.IOLoop.instance()
        delay = 0.1
        thumbnail_filepath = None
        while True:
            yield tornado.gen.Task(
                ioloop_instance.add_timeout,
                time.time() + delay
            )
            delay *= 2
            if job.result is not None:
                thumbnail_filepath = job.result
                break
            elif delay > 2:
                break

        if extension == 'png':
            self.set_header('Content-Type', 'image/png')
        elif extension == 'jpg':
            self.set_header('Content-Type', 'image/jpeg')
        else:
            raise ValueError(extension)

        if not thumbnail_filepath:
            self.set_header('Content-Type', 'image/png')
            thumbnail_filepath = os.path.join(
                self.application.settings['static_path'],
                'images',
                'file_broken.png'
            )
            self.set_header(
                'Cache-Control',
                'max-age=0'
            )
        else:
            _cache_seconds = 60 * 60 * 24
            self.set_header(
                'Cache-Control',
                'max-age=%d, public' % _cache_seconds
            )
            if _cache_seconds > 3600:
                _expires = (
                    datetime.datetime.utcnow() +
                    datetime.timedelta(seconds=_cache_seconds)
                )
                self.set_header(
                    'Expires',
                    _expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
                )
        self.write(open(thumbnail_filepath, 'rb').read())
        self.finish()


@route(r'/preload-urls/(?P<fileid>\w{9})', 'preload-urls')
class PreloadURLsHandler(BaseHandler):

    def get(self, fileid):
        root = self.application.settings['static_path']
        path = os.path.join(root, 'tiles')
        image_filename = (
            fileid[:1] +
            '/' +
            fileid[1:3] +
            '/' +
            fileid[3:]
        )
        path = os.path.join(path, image_filename)
        path = os.path.join(path, '256', str(self.DEFAULT_ZOOM))

        urls = []
        if os.path.isdir(path):
            for f in os.listdir(path):
                urls.append(os.path.join(path, f).replace(
                    self.application.settings['static_path'],
                    ''
                ))

        self.write({'urls': urls})


@route(r'/about', 'about')
class AboutHandler(BaseHandler):

    def get(self):
        self.render('about.html')


@route(r'/privacy', 'privacy')
class PrivacyHandler(BaseHandler):

    def get(self):
        self.render('privacy.html')


@route(r'/gettingstarted', 'gettingstarted')
class GettingStartedHandler(BaseHandler):

    def get(self):
        self.render('gettingstarted.html')


# this handler gets automatically appended last to all handlers inside app.py
class PageNotFoundHandler(BaseHandler):

    def get(self):
        path = self.request.path
        page = path[1:]
        if page.endswith('/'):
            page = page[:-1]
        page = page.split('/')[-1]
        if not path.endswith('/'):
            new_url = '%s/' % path
            if self.request.query:
                new_url += '?%s' % self.request.query
            self.redirect(new_url)
            return
        raise tornado.web.HTTPError(404, path)
