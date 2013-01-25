# coding=utf-8
import copy
import functools, datetime, decimal
from functools import update_wrapper
from inspect import getargspec

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.template import Context, Template
from django.template.response import TemplateResponse
from django.utils import simplejson
from django.utils.datastructures import SortedDict
from django.utils.decorators import method_decorator, classonlymethod
from django.utils.encoding import smart_unicode
from django.utils.http import urlencode
from django.utils.itercompat import is_iterable
from django.utils.safestring import mark_safe
from django.utils.text import capfirst
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_protect
from django.views.generic import View
from exadmin.util import static

#: 通用的csrf_protect_m装饰器，给其他模块的 AdminView 使用
csrf_protect_m = method_decorator(csrf_protect)

class IncorrectPluginArg(Exception):
    pass

def filter_chain(filters, token, func, *args, **kwargs):
    if token == -1:
        return func()
    else:
        def _inner_method():
            fm = filters[token]
            fargs = getargspec(fm)[0]
            if len(fargs) == 1:
                # Only self arg
                result = func()
                if result is None:
                    return fm()
                else:
                    raise IncorrectPluginArg(_(u'Plugin filter method need a arg to receive parent method result.'))
            else:
                return fm(func if fargs[1] == '__' else func(), *args, **kwargs)
        return filter_chain(filters, token-1, _inner_method, *args, **kwargs)

def filter_hook(func):
    tag = func.__name__
    func.__doc__ = "``filter_hook``\n\n" + (func.__doc__ or "")
    @functools.wraps(func)
    def method(self, *args, **kwargs):

        def _inner_method():
            return func(self, *args, **kwargs)

        if self.plugins:
            filters = [(getattr(getattr(p, tag), 'priority', 10), getattr(p, tag)) \
                for p in self.plugins if callable(getattr(p, tag, None))]
            filters = [f for p,f in sorted(filters, key=lambda x:x[0])]
            return filter_chain(filters, len(filters)-1, _inner_method, *args, **kwargs)
        else:
            return _inner_method()
    return method

def inclusion_tag(file_name, context_class=Context, takes_context=False):
    """
    为 AdminView 的 block views 提供的便利方法，作用等同于 :meth:`django.template.Library.inclusion_tag`
    """
    def wrap(func):
        @functools.wraps(func)
        def method(self, context, nodes, *arg, **kwargs):
            _dict  = func(self, context, nodes, *arg, **kwargs)
            from django.template.loader import get_template, select_template
            if isinstance(file_name, Template):
                t = file_name
            elif not isinstance(file_name, basestring) and is_iterable(file_name):
                t = select_template(file_name)
            else:
                t = get_template(file_name)
            new_context = context_class(_dict, **{
                'autoescape': context.autoescape,
                'current_app': context.current_app,
                'use_l10n': context.use_l10n,
                'use_tz': context.use_tz,
            })
            new_context['admin_view'] = context['admin_view']
            csrf_token = context.get('csrf_token', None)
            if csrf_token is not None:
                new_context['csrf_token'] = csrf_token
            nodes.append(t.render(new_context))

        return method
    return wrap

class JSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.date):
            return o.strftime('%Y-%m-%d')
        elif isinstance(o, datetime.datetime):
            return o.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(o, decimal.Decimal):
            return str(o)
        else:
            try:
                return super(JSONEncoder, self).default(o)
            except Exception:
                return smart_unicode(o)

class BaseAdminObject(object):
    """
    提供给 :class:`BaseAdminView` 和 :class:`BaseAdminPlugin` 的通用基类，主要是提供了一些常用的通用方法
    """
    def get_view(self, view_class, option_class=None, *args, **kwargs):
        """
        获取 AdminViewClass 的实例。实际上就是调用 :meth:`~exadmin.sites.AdminSite.get_view_class` 方法

        :param view_class: AdminViewClass 的类
        :param option_class: 希望与 AdminViewClass 合并的 OptionClass
        """
        opts = kwargs.pop('opts', {})
        return self.admin_site.get_view_class(view_class, option_class, **opts)(self.request, *args, **kwargs)

    def get_model_view(self, view_class, model, *args, **kwargs):
        """
        获取 ModelAdminViewClass 的实例。首先通过 :class:`~exadmin.sites.AdminSite` 取得 model 对应的 OptionClass，然后调用 :meth:`get_view` 方法

        :param view_class: ModelAdminViewClass 的类
        :param model: 绑定的 Model 类
        """
        return self.get_view(view_class, self.admin_site._registry.get(model), *args, **kwargs)

    def get_admin_url(self, name, *args, **kwargs):
        """
        便捷方法，方便的通过 name 取得 url，会加上 AdminSite.app_name 的 url namespace
        """
        return reverse('%s:%s' % (self.admin_site.app_name, name), args=args, kwargs=kwargs)

    def get_model_url(self, model, name, *args, **kwargs):
        """
        便捷方法，方便的通过 model, name 取得 url，会自动拼成 urlname，并会加上 AdminSite.app_name 的 url namespace
        """
        return reverse('%s:%s_%s_%s' % (self.admin_site.app_name, model._meta.app_label, model._meta.module_name, name), \
            args=args, kwargs=kwargs, current_app=self.admin_site.name)

    def get_model_perm(self, model, name):
        """
        获取 Model 的某种权限标签，标签的格式为::

            >>> view.get_model_perm(User, 'view')
            >>> 'auth.user_view'
        """
        return '%s.%s_%s' % (model._meta.app_label, name, model._meta.module_name)

    def has_model_perm(self, model, name, user=None):
        """
        判断当前用户是否有某个 Model 的 某种权限，例如:

            >>> view.has_model_perm(User, 'view')
            >>> True
        """
        user = user or self.user
        return user.has_perm(self.get_model_perm(model, name)) or (name == 'view' and self.has_model_perm(model, 'change', user))

    def get_query_string(self, new_params=None, remove=None):
        """
        在当前的query_string基础上生成新的query_string

        :param new_params: 要新加的参数，该参数为 dict 
        :param remove: 要删除的参数，该参数为 list, tuple
        """
        if new_params is None: new_params = {}
        if remove is None: remove = []
        p = dict(self.request.GET.items()).copy()
        for r in remove:
            for k in p.keys():
                if k.startswith(r):
                    del p[k]
        for k, v in new_params.items():
            if v is None:
                if k in p:
                    del p[k]
            else:
                p[k] = v
        return '?%s' % urlencode(p)

    def get_form_params(self, new_params=None, remove=None):
        """
        将当前 request 的参数，新加或是删除后，生成 hidden input。用于放入 HTML 的 Form 中。

        :param new_params: 要新加的参数，该参数为 dict 
        :param remove: 要删除的参数，该参数为 list, tuple
        """
        if new_params is None: new_params = {}
        if remove is None: remove = []
        p = dict(self.request.GET.items()).copy()
        for r in remove:
            for k in p.keys():
                if k.startswith(r):
                    del p[k]
        for k, v in new_params.items():
            if v is None:
                if k in p:
                    del p[k]
            else:
                p[k] = v
        return mark_safe(''.join(
            '<input type="hidden" name="%s" value="%s"/>' % (k, v) for k,v in p.items() if v))

    def render_response(self, content, response_type='json'):
        """
        便捷方法，方便生成 HttpResponse，如果 response_type 为 ``json`` 会自动转为 json 格式后输出
        """
        if response_type == 'json':
            response = HttpResponse(mimetype="application/json; charset=UTF-8")
            json = simplejson.dumps(content, cls=JSONEncoder, ensure_ascii=False)
            response.write(json)
            return response
        return HttpResponse(content)

    def template_response(self, template, context):
        """
        便捷方法，方便生成 TemplateResponse
        """
        return TemplateResponse(self.request, template, context, current_app=self.admin_site.name)

    def static(self, path):
        """
        :meth:`exadmin.util.static` 的快捷方法，返回静态文件的 url。
        """
        return static(path)

class BaseAdminPlugin(BaseAdminObject):
    """
    所有 Plugin 的基类。继承于 :class:`BaseAdminObject`
    """
    def __init__(self, admin_view):
        self.admin_view = admin_view
        self.admin_site = admin_view.admin_site

        if hasattr(admin_view, 'model'):
            self.model = admin_view.model
            self.opts = admin_view.model._meta

    def init_request(self, *args, **kwargs):
        """
        插件的初始化方法，Plugin 实例化后被调用的第一个方法。该方法主要用于初始化插件需要的属性，
        同时判断当前请求是否需要加载该插件，例如 Ajax插件的实现方式::

            def init_request(self, *args, **kwargs):
                return bool(self.request.is_ajax() or self.request.REQUEST.get('_ajax'))

        当返回值为 ``False`` 时，所属的 AdminView 实例不会加载该插件
        """
        pass

class BaseAdminView(BaseAdminObject, View):
    """ Base Admin view, support some comm attrs."""

    def __init__(self, request, *args, **kwargs):
        self.request = request
        self.request_method = request.method.lower()
        self.user = request.user

        self.base_plugins = [p(self) for p in getattr(self, "plugin_classes", [])]

        self.args = args
        self.kwargs = kwargs
        self.init_plugin(*args, **kwargs)
        self.init_request(*args, **kwargs)

    @classonlymethod
    def as_view(cls):
        def view(request, *args, **kwargs):
            self = cls(request, *args, **kwargs)

            if hasattr(self, 'get') and not hasattr(self, 'head'):
                self.head = self.get

            if self.request_method in self.http_method_names:
                handler = getattr(self, self.request_method, self.http_method_not_allowed)
            else:
                handler = self.http_method_not_allowed

            return handler(request, *args, **kwargs)

        # take name and docstring from class
        update_wrapper(view, cls, updated=())
        # and possible attributes set by decorators
        # like csrf_exempt from dispatch
        update_wrapper(view, cls.dispatch, assigned=())
        return view

    def init_request(self, *args, **kwargs):
        pass

    def init_plugin(self, *args, **kwargs):
        plugins = []
        for p in self.base_plugins:
            p.request = self.request
            p.user = self.user
            p.args = self.args
            p.kwargs = self.kwargs
            result = p.init_request(*args, **kwargs)
            if result is not False:
                plugins.append(p)
        self.plugins = plugins

    @filter_hook
    def get_context(self):
        return {'admin_view': self, 'media': self.media}

    @property
    def media(self):
        return self.get_media()

    @filter_hook
    def get_media(self):
        return forms.Media()

class CommAdminView(BaseAdminView):

    site_title = None
    globe_models_icon = {}

    def get_site_menu(self):
        return None

    @filter_hook
    def get_nav_menu(self):
        site_menu = list(self.get_site_menu() or [])
        had_urls = []

        def get_url(menu, had_urls):
            if menu.has_key('url'):
                had_urls.append(menu['url'])
            if menu.has_key('menus'):
                for m in menu['menus']:
                    get_url(m, had_urls)
        get_url({'menus': site_menu}, had_urls)

        nav_menu = SortedDict()

        for model, model_admin in self.admin_site._registry.items():
            app_label = model._meta.app_label

            model_dict = {
                'title': unicode(capfirst(model._meta.verbose_name_plural)),
                'url': self.get_model_url(model, "changelist"),
                'icon': self.get_model_icon(model),
                'perm': self.get_model_perm(model, 'view')
            }
            if model_dict['url'] in had_urls:
                continue

            app_key = "app:%s" % app_label
            if app_key in nav_menu:
                nav_menu[app_key]['menus'].append(model_dict)
            else:
                nav_menu[app_key] = {
                    'title': unicode(app_label.title()),
                    'menus': [model_dict],
                }

        for menu in nav_menu.values():
            menu['menus'].sort(key=lambda x: x['title'])

        nav_menu = nav_menu.values()
        nav_menu.sort(key=lambda x: x['title'])

        site_menu.extend(nav_menu)

        return site_menu

    @filter_hook
    def get_context(self):
        context = super(CommAdminView, self).get_context()

        if not settings.DEBUG and self.request.session.has_key('nav_menu'):
            nav_menu = simplejson.loads(self.request.session['nav_menu'])
        else:
            menus = copy.copy(self.get_nav_menu())
            
            def check_menu_permission(item):
                need_perm = item.pop('perm', None)
                if need_perm is None:
                    return True
                elif callable(need_perm):
                    return need_perm(self.user)
                elif need_perm == 'super':
                    return self.user.is_superuser
                else:
                    return self.user.has_perm(need_perm)

            def filter_item(item):
                if item.has_key('menus'):
                    item['menus'] = [filter_item(i) for i in item['menus'] if check_menu_permission(i)]
                return item

            nav_menu = [filter_item(item) for item in menus if check_menu_permission(item)]
            nav_menu = filter(lambda i: bool(i['menus']), nav_menu)

            if not settings.DEBUG:
                self.request.session['nav_menu'] = simplejson.dumps(nav_menu)
                self.request.session.modified = True

        def check_selected(menu, path):
            selected = menu.has_key('url') and path.startswith(menu['url']) or False
            if menu.has_key('menus'):
                for m in menu['menus']:
                    _s = check_selected(m, path)
                    if _s: selected = True
            if selected: menu['selected'] = True
            return selected
        for menu in nav_menu:
            check_selected(menu, self.request.path)

        context['nav_menu'] = nav_menu
        context['site_title'] = self.site_title or _(u'Django Xadmin')
        return context

    @filter_hook
    def get_model_icon(self, model):
        icon = self.globe_models_icon.get(model)
        if icon is None and model in self.admin_site._registry:
            icon = getattr(self.admin_site._registry[model], 'model_icon', None)
        return icon

    @filter_hook
    def message_user(self, message, level='info'):
        """
        Send a message to the user. The default implementation
        posts a message using the django.contrib.messages backend.
        """
        if hasattr(messages, level) and callable(getattr(messages, level)):
            getattr(messages, level)(self.request, message)


class ModelAdminView(CommAdminView):

    fields = None
    exclude = None
    ordering = None
    model = None

    def __init__(self, request, *args, **kwargs):
        self.opts = self.model._meta
        self.app_label = self.model._meta.app_label
        self.module_name = self.model._meta.module_name
        self.model_info = (self.app_label, self.module_name)

        super(ModelAdminView, self).__init__(request, *args, **kwargs)

    @filter_hook
    def get_object(self, object_id):
        """
        Get model object instance by object_id, used for change admin view
        """
        # first get base admin view property queryset, return default model queryset
        queryset = self.queryset()
        model = queryset.model
        try:
            object_id = model._meta.pk.to_python(object_id)
            return queryset.get(pk=object_id)
        except (model.DoesNotExist, ValidationError):
            return None

    def model_admin_urlname(self, name, *args, **kwargs):
        return reverse("%s:%s_%s_%s" % (self.admin_site.app_name, self.opts.app_label, \
            self.module_name, name), args=args, kwargs=kwargs)

    def get_model_perms(self):
        """
        Returns a dict of all perms for this model. This dict has the keys
        ``add``, ``change``, and ``delete`` mapping to the True/False for each
        of those actions.
        """
        return {
            'view': self.has_view_permission(),
            'add': self.has_add_permission(),
            'change': self.has_change_permission(),
            'delete': self.has_delete_permission(),
        }

    def get_template_list(self, template_name):
        opts = self.opts
        return (
            "admin/%s/%s/%s" % (opts.app_label, opts.object_name.lower(), template_name),
            "admin/%s/%s" % (opts.app_label, template_name),
            "admin/%s" % template_name,
        )

    def get_ordering(self):
        """
        Hook for specifying field ordering.
        """
        return self.ordering or ()  # otherwise we might try to *None, which is bad ;)
        
    def queryset(self):
        """
        Returns a QuerySet of all model instances that can be edited by the
        admin site. This is used by changelist_view.
        """
        return self.model._default_manager.get_query_set()

    def has_view_permission(self, obj=None):
        return self.user.has_perm('%s.view_%s'% self.model_info) or self.has_change_permission(obj)

    def has_add_permission(self):
        return self.user.has_perm('%s.add_%s'% self.model_info)

    def has_change_permission(self, obj=None):
        return self.user.has_perm('%s.change_%s'% self.model_info)

    def has_delete_permission(self, obj=None):
        return self.user.has_perm('%s.delete_%s'% self.model_info)

