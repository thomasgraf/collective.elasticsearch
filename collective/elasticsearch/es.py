from logging import getLogger
import traceback

from Acquisition import aq_base
from DateTime import DateTime
from Products.ZCatalog.Lazy import LazyMap
from Products.CMFCore.utils import _getAuthenticatedUser
from Products.CMFCore.permissions import AccessInactivePortalContent
from Products.CMFCore.utils import _checkPermission
from zope.component import getUtility
from zope.component import queryMultiAdapter
from zope.component import ComponentLookupError

from plone.registry.interfaces import IRegistry
from plone.indexer.interfaces import IIndexableObject

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError, ElasticsearchException

from collective.elasticsearch.brain import BrainFactory
from collective.elasticsearch.query import QueryAssembler
from collective.elasticsearch.interfaces import (
    IElasticSettings, DISABLE_MODE, DUAL_MODE)
from collective.elasticsearch.utils import getUID
from collective.elasticsearch.indexes import getIndex
from collective.elasticsearch import td


logger = getLogger(__name__)
info = logger.info
warn = logger.warn

CONVERTED_ATTR = '_elasticconverted'


class PatchCaller(object):
    """
    Very odd I have to do this. If I don't,
    I get very pecular errors trying to call
    the original methods
    """

    def __init__(self, patched_object):
        self._patched_object = patched_object

    def __getattr__(self, name):
        """
        assuming original attribute has "__old_" prefix
        """
        if name[0] == '_':
            return self.__dict__[name]
        _type = type(aq_base(self._patched_object))
        func = getattr(_type, '__old_' + name)

        # "bind" it
        def bound_func(*args, **kwargs):
            return func(self._patched_object, *args, **kwargs)
        return bound_func


class ElasticSearch(object):

    def __init__(self, catalogtool):
        self.catalogtool = catalogtool
        self.catalog = catalogtool._catalog
        self.patched = PatchCaller(self.catalogtool)

        try:
            registry = getUtility(IRegistry)
            try:
                self.registry = registry.forInterface(IElasticSettings)
            except:
                self.registry = None
        except ComponentLookupError:
            self.registry = None

        self.tdata = td.get()

    @property
    def catalog_converted(self):
        return getattr(self.catalogtool, CONVERTED_ATTR, False)

    @property
    def mode(self):
        if not self.catalog_converted:
            return DISABLE_MODE
        if self.registry is None:
            return DISABLE_MODE
        return self.registry.mode

    def get_setting(self, name, default=None):
        return getattr(self.registry, name, default)

    @property
    def conn(self):
        if self.tdata.conn is None:
            self.tdata.conn = Elasticsearch(
                self.registry.hosts,
                timeout=self.get_setting('timeout', 0.5),
                sniff_on_start=self.get_setting('sniff_on_start', False),
                sniff_on_connection_fail=self.get_setting('sniff_on_connection_fail',
                                                          False),
                sniffer_timeout=self.get_setting('sniffer_timeout', 0.1),
                retry_on_timeout=self.get_setting('retry_on_timeout', False))
        return self.tdata.conn

    def _es_search(self, query, **query_params):
        """
        override default es search to use our own ResultSet class that works
        """
        if "start" in query_params:
            query_params['from_'] = query_params.pop("start")
        query_params['fields'] = 'path.path'

        return self.conn.search(index=self.catalogsid,
                                doc_type=self.catalogtype,
                                body={'query': query},
                                **query_params)

    def query(self, query):
        qassembler = QueryAssembler(self.catalogtool)
        dquery, sort = qassembler.normalize(query)
        equery = qassembler(dquery)
        result = self._es_search(equery, sort=sort)['hits']
        count = result['total']
        factory = BrainFactory(self.catalog)
        return LazyMap(factory, result['hits'], count)

    def catalog_object(self, obj, uid=None, idxs=[],
                       update_metadata=1, pghandler=None):
        mode = self.mode
        if mode in (DISABLE_MODE, DUAL_MODE):
            result = self.patched.catalog_object(
                obj, uid, idxs, update_metadata, pghandler)
            if mode == DISABLE_MODE:
                return result
        wrapped_object = None
        if not IIndexableObject.providedBy(obj):
            # This is the CMF 2.2 compatible approach, which should be used
            # going forward
            wrapper = queryMultiAdapter((obj, self.catalogtool),
                                        IIndexableObject)
            if wrapper is not None:
                wrapped_object = wrapper
            else:
                wrapped_object = obj
        else:
            wrapped_object = obj
        conn = self.conn
        catalog = self.catalog
        if idxs == []:
            idxs = catalog.indexes.keys()
        index_data = {}
        for index_name in idxs:
            index = getIndex(catalog, index_name)
            if index is not None:
                value = index.get_value(wrapped_object)
                if value in (None, 'None'):
                    # yes, we'll index null data...
                    value = None
                index_data[index_name] = value
        if update_metadata:
            index = self.catalog.uids.get(uid, None)
            if index is None:  # we are inserting new data
                index = self.catalog.updateMetadata(obj, uid, None)
                self.catalog._length.change(1)
                self.catalog.uids[uid] = index
                self.catalog.paths[index] = uid
            # need to match elasticsearch result with brain
            self.catalog.updateMetadata(wrapped_object, uid, index)

        uid = getUID(obj)
        try:
            doc = conn.get(index=self.catalogsid, doc_type=self.catalogtype, id=uid)
            self.registerInTransaction(uid, td.Actions.modify, doc['_source'])
            doc = doc.copy()  # we copy so we can update safely
            doc.update(index_data)
        except NotFoundError:
            self.registerInTransaction(uid, td.Actions.add)
            doc = index_data
        conn.index(body=doc, index=self.catalogsid, doc_type=self.catalogtype, id=uid)

        if self.registry.auto_flush:
            conn.indices.refresh(index=self.catalogsid)

    def registerInTransaction(self, uid, action, doc={}):
        if not self.tdata.registered:
            self.tdata.register(self)
        self.tdata.docs.append(
            (action, uid, doc)
        )

    def uncatalog_object(self, uid, obj=None, *args, **kwargs):
        mode = self.mode
        if mode in (DISABLE_MODE, DUAL_MODE):
            if self.catalog.uids.get(uid, None) is not None:
                result = self.patched.uncatalog_object(uid, *args, **kwargs)
            if mode == DISABLE_MODE:
                return result
        conn = self.conn

        uid = getUID(obj)
        try:
            doc = conn.get(index=self.catalogsid, doc_type=self.catalogtype, id=uid)
            self.registerInTransaction(uid, td.Actions.delete, doc['_source'])
        except NotFoundError:
            pass
        try:
            conn.delete(index=self.catalogsid, doc_type=self.catalogtype, id=uid)
        except NotFoundError:
            # already gone... Multiple calls?
            pass
        if self.registry.auto_flush:
            conn.indices.refresh(index=self.catalogsid)

    def manage_catalogRebuild(self, *args, **kwargs):
        mode = self.mode
        if mode == DISABLE_MODE:
            return self.patched.manage_catalogRebuild(*args, **kwargs)

        self.recreateCatalog()

        return self.patched.manage_catalogRebuild(*args, **kwargs)

    def manage_catalogClear(self, *args, **kwargs):
        mode = self.mode
        if mode == DISABLE_MODE:
            return self.patched.manage_catalogClear(*args, **kwargs)

        self.recreateCatalog()

        if mode == DUAL_MODE:
            return self.patched.manage_catalogClear(*args, **kwargs)

    def refreshCatalog(self, clear=0, pghandler=None):
        mode = self.mode
        if mode == DISABLE_MODE:
            return self.patched.refreshCatalog(clear, pghandler)

        return self.patched.refreshCatalog(clear, pghandler)

    def recreateCatalog(self):
        conn = self.conn
        try:
            conn.indices.delete(index=self.catalogsid)
        except NotFoundError:
            pass
        self.convertToElastic()

    def searchResults(self, REQUEST=None, check_perms=False, **kw):
        mode = self.mode
        if mode == DISABLE_MODE:
            return self.patched.searchResults(REQUEST, **kw)
        if isinstance(REQUEST, dict):
            query = REQUEST.copy()
        else:
            query = {}
        query.update(kw)

        if check_perms:
            show_inactive = query.get('show_inactive', False)
            if isinstance(REQUEST, dict) and not show_inactive:
                show_inactive = 'show_inactive' in REQUEST

            user = _getAuthenticatedUser(self.catalogtool)
            query['allowedRolesAndUsers'] = \
                self.catalogtool._listAllowedRolesAndUsers(user)

            if not show_inactive and not _checkPermission(
                    AccessInactivePortalContent, self.catalogtool):
                query['effectiveRange'] = DateTime()
        orig_query = query.copy()
        # info('Running query: %s' % repr(orig_query))
        try:
            return self.query(query)
        except:
            info("Error running Query: %s\n%s" % (
                repr(orig_query),
                traceback.format_exc()))
            if mode == DUAL_MODE:
                # fall back now...
                return self.patched.searchResults(REQUEST, **kw)
            else:
                return LazyMap(BrainFactory(self.catalog), [], 0)

    def convertToElastic(self):
        setattr(self.catalogtool, CONVERTED_ATTR, True)
        self.catalogtool._p_changed = True
        properties = {}
        for name in self.catalog.indexes.keys():
            index = getIndex(self.catalog, name)
            if index is not None:
                properties[name] = index.create_mapping(name)
            else:
                raise Exception("Can not locate index for %s" % (
                    name))

        conn = self.conn
        try:
            conn.indices.create(self.catalogsid)
        except ElasticsearchException:
            pass

        mapping = {'properties': properties}
        conn.indices.put_mapping(
            doc_type=self.catalogtype,
            body=mapping,
            index=self.catalogsid)

    @property
    def catalogsid(self):
        return '-'.join(self.catalogtool.getPhysicalPath()[1:]).lower()

    @property
    def catalogtype(self):
        return self.catalogtool.getId().lower()
