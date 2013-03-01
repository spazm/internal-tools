import inspect
import re
import sys
from django.db.utils import IntegrityError, DatabaseError
from django.db import models, transaction
from django.utils.translation import ugettext_lazy as _
from south.modelsinspector import add_introspection_rules
import urllib2

from configs.settings.ldap import INACTIVE_USER_STRING, CONTRACTOR_STRING

GUARANTEE_INSTANCE_BY_NAME = False

# TODO(kevinx): Move to common location?
BASE_PROFILE_PHOTO_HOST = 'http://intranet'
BASE_PROFILE_PHOTO_PATH = '/badge/'
DEFAULT_PROFILE_PHOTO_EXTENSION = '.jpg'

# Prefixes:
# fk_ = foreign key
# pk_ = primary key
# Suffixes:
# _const = uneditable after initial save
# _gen = autogenerated by system or external data
# _opt = optional


def camel_to_delimited(text, delimiter='_', lowered=True):
    return_text = ''
    for i, l in enumerate(text):
        if l.upper() == l and i != 0:
            return_text += delimiter + (lowered and l.lower() or l)
        else:
            return_text += lowered and l.lower() or l
    return return_text


class NowDateTimeField(models.DateTimeField):
    """
    Allow the "now()" string into date, bypassing validation and save
    directly into SQL statement.
    """
    @staticmethod
    def is_now(now):
        return isinstance(now, basestring) and now.lower() == 'now()'

    def to_python(self, value):
        """ Override the superclass """
        if self.is_now(value):
            return value
            # the superclass returns a datetime.datetime object
        return super(NowDateTimeField, self).to_python(value)

    def value_to_string(self, value):
        """ Override the superclass """
        if self.is_now(value):
            return value
        return super(NowDateTimeField, self).value_to_string(value)

# tell South to obtain freezing rules from inherited class
# see: http://south.aeracode.org/wiki/MyFieldsDontWork
add_introspection_rules([], ["^dssodjango\.models\.NowDateTimeField"])


class CommonModel(models.Model):
    def __init__(self, *args, **kwargs):
        # take in {'paramname': 'value', ...} and map it to:
        # {'paramname': models.<Model>(name=value)}
        if GUARANTEE_INSTANCE_BY_NAME:
            for param_name, instance_or_string in kwargs.iteritems():
                if (isinstance(instance_or_string, basestring) and
                        param_name in PARAMNAME_TO_CLASS_MAP):
                    clz = PARAMNAME_TO_CLASS_MAP[param_name]
                    kwargs[param_name] = (
                        clz.get_guaranteed_instance_by_name(instance_or_string))

        # children and parents specifies hierarchy (allows cascading)
        super(CommonModel, self).__init__(*args, **kwargs)
        self.parents = ()
        self.children = ()

    def get_ancestors(self):
        parent_dict = {}
        for P in self.parents:
            _p_col = camel_to_delimited(P.__name__)
            parent = getattr(self, _p_col)
            parent_dict.update({_p_col: parent})
            parent_dict.update(CommonModel.get_ancestors(parent))
        return parent_dict

    cache_by_name = {}

    @classmethod
    def get_instance_by_name(cls, name):
        """ Get an instance, could throw cls.DoesNotExist exception """

        # key must be a composite of class:name because cache_by_name is
        # shared amongst all the children!
        key = "%s:%s" % (cls.__name__, name)
        if key in cls.cache_by_name:
            return cls.cache_by_name[key]
        try:
            model_obj = cls.objects.get(name__iexact=name)
            cls.cache_by_name[key] = model_obj
            return model_obj
        except cls.DoesNotExist, e:
            raise cls.DoesNotExist(
                "Unable to find instance %s with name '%s' (%s)" % (
                cls.__name__, name, e))

    @classmethod
    @transaction.autocommit
    def get_guaranteed_instance_by_name(cls, name):
        try:
            model = cls.get_instance_by_name(name)
        except cls.DoesNotExist:
            try:
                model = cls(name=name)
                model.save()
            except IntegrityError:
                transaction.rollback()
                # this is possible if save() race condition occurs
                model = cls.get_instance_by_name(name)
            except DatabaseError as e:
                raise DatabaseError("Unable to save %s:[%s] (%s)" % (
                    cls, name, e))

        return model

    created_at = NowDateTimeField(auto_now_add=True)
    updated_at = NowDateTimeField(auto_now=True)

    objects = models.Manager()  # default

    class Meta:
        abstract = True


class SSOAuthInfo(CommonModel):
    """
    SSO authentication container
    """
    def __init__(self, *args, **kwargs):
        super(CommonModel, self).__init__(*args, **kwargs)
        #self.parent = (PartnerData,)

    id = models.AutoField(primary_key=True, db_column='pk_authinfo_id')

    auth_key = models.CharField(max_length=64,
                                unique=True,
                                db_index=True)
    browser = models.CharField(max_length=512)

    # LDAP information
    cn = models.CharField(max_length=256, null=False)  # canonical name
    displayName = models.CharField(max_length=128, null=False)
    mail = models.CharField(max_length=64, null=False)
    objectSid = models.CharField(max_length=128, null=False)

    # services that are logged in under this key
    services = models.CharField(max_length=4096)

    last_service = models.CharField(max_length=64)

    # displayName
    # cn
    # full CN
    # title, description
    # department
    # mail
    # manager (full CN)
    # objectCategory:['CN=Person,CN=Schema,CN=Configuration,DC=dm,DC=local']
    # streetAddress
    # st (state)
    # telephoneNumber
    # whenCreated

    #fetch_unixtimestamp = NowDateTimeField(null=True,
    #                                       blank=True,
    #                                       db_column='fetch_unixtimestamp')
    def __unicode__(self):
        return "%s(%s)" % (self.name, self.id)


class SSOAppToken(CommonModel):
    """
    The tokens are transient and should only be accessed by services
    """
    token = models.CharField(primary_key=True,
                             max_length=48,
                             unique=True,
                             db_index=True)
    service = models.CharField(max_length=64)

    ssoauthinfo = models.ForeignKey(SSOAuthInfo,
                                    db_column='fk_ssoauthinfo_id',
                                    null=False,
                                    blank=False)


class AppAuthInfo(CommonModel):
    app_auth_key = models.CharField(primary_key=True,
                                    max_length=64,
                                    unique=True,
                                    db_index=True)

    cn = models.CharField(max_length=256, null=False)  # canonical name
    displayName = models.CharField(max_length=128, null=False)
    mail = models.CharField(max_length=64, null=False)
    objectSid = models.CharField(max_length=128, null=False)


class URL(CommonModel):
    """
    http://go URL shortener
    """
    id = models.AutoField(primary_key=True, db_column='pk_url_id')
    short_url = models.CharField(max_length=80,
                                 unique=True,
                                 db_index=True)
    long_url = models.CharField(max_length=8192,
                                db_index=True)
    username = models.CharField(max_length=48, db_index=True)

    # statistics
    total_clicks = models.PositiveIntegerField(null=False, blank=False,
                                               default=0)
    last_month_clicks = models.PositiveIntegerField(null=False,
                                                    blank=False,
                                                    default=0,
                                                    db_index=True)
    decayed_clicks = models.PositiveIntegerField(null=False,
                                                 blank=False,
                                                 default=0,
                                                 db_index=True)


class LDAPUser(CommonModel):
    # roughly ordered by the objectSid and hire_date
    #order_id = models.PositiveIntegerField(blank=True, db_index=True)

    # low level LDAP information
    cn = models.CharField(max_length=256, null=False,
                          db_index=True, unique=True)  # canonical name
    displayName = models.CharField(max_length=128, null=False, db_index=True)
    mail = models.CharField(max_length=64, null=False)
    #objectSid = models.CharField(max_length=128, null=False, db_index=True)

    @property
    def username(self):
        """ This should be the same as ldap_username """
        return re.sub(r'@.+', '', self.mail)

    manager = models.ForeignKey('self',
                                related_name='direct_reports',
                                blank=True, null=True,
                                db_index=True)  # defined from LDAP manager

    @property
    def reports(self):
        return LDAPUser.objects.filter(manager__id=self.id)

    # Sync this with Mike Reinhardt's LDAPUser
    # User class with LDAP fields.
    address = models.CharField(_('address'), max_length=120, default='',
                               blank=True)  # ldap: streetAddress
    city = models.CharField(_('city'), max_length=120, default='',
                            blank=True)  # ldap: physicalDeliveryOfficeName
    department = models.CharField(_('department'), max_length=120, default='',
                                  blank=True,
                                  db_index=True)
    #hire_date = models.PositiveIntegerField(_('hire date'), blank=True)
    hire_date = NowDateTimeField(blank=True, db_index=True)
    # ldap: whenCreated
    ldap_username = models.CharField(_('LDAP username'), max_length=120,
                                     default='', blank=True)  # ldap: mailNickname
    location = models.CharField(_('location'), max_length=120, default='',
                                blank=True)  # ldap: l
    phone = models.CharField(_('phone'), max_length=60, default='', blank=True)
    # ldap: telephoneNumber
    # some people have "New York" as state!
    state = models.CharField(_('state'), max_length=40, default='', blank=True)
    # ldap: st
    title = models.CharField(_('title'), max_length=120, default='',
                             blank=True,
                             db_index=True)
    zip_code = models.CharField(_('zip code'), max_length=12, default='',
                                blank=True)  # ldap: postalCode

    @property
    def is_active(self):
        return not re.search(INACTIVE_USER_STRING, self.cn)

    @property
    def is_contractor(self):
        return not re.search(CONTRACTOR_STRING, self.cn)

    @property
    def photo_url(self):
        username_split = self.username.split('.')
        if len(username_split) < 2:
            return ''

        # check that intranet badge photo exists, otherwise use default photo
        desired_photo_url = ''.join((
            BASE_PROFILE_PHOTO_HOST, BASE_PROFILE_PHOTO_PATH,
            username_split[1].lower(), username_split[0].lower(),
            DEFAULT_PROFILE_PHOTO_EXTENSION))

        return desired_photo_url

    @property
    def photo_url_with_check(self):
        username_split = self.username.split('.')

        # check that intranet badge photo exists, otherwise use default photo
        desired_photo_url = ''.join((
            BASE_PROFILE_PHOTO_HOST, BASE_PROFILE_PHOTO_PATH,
            username_split[1].lower(), username_split[0].lower(),
            DEFAULT_PROFILE_PHOTO_EXTENSION))
        try:
            connection = urllib2.urlopen(desired_photo_url)
            status = connection.getcode()
            connection.close()
        except urllib2.HTTPError, e:
            status = e.getcode()

        if status == 200:
            photo = desired_photo_url
        else:
            photo = MISSING_PHOTO_URL

        return photo

    #class Meta:
    #    abstract = True


def get_paramname_to_class():
    """
    Inspect this file, and map all the lower case <parametername>
    to the actual class models.Model.<ParameterName>
    """
    paramname_to_class = {}
    this_file = sys.modules[__name__]
    for clz_name in dir(this_file):
        model_clz = getattr(this_file, clz_name)
        if inspect.isclass(model_clz) and issubclass(model_clz, models.Model):
            paramname_to_class[clz_name.lower()] = model_clz
    return paramname_to_class

PARAMNAME_TO_CLASS_MAP = get_paramname_to_class()