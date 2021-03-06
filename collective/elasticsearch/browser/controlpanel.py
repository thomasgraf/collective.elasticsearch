from collective.elasticsearch.es import ElasticSearchCatalog
from collective.elasticsearch.interfaces import IElasticSettings
from plone.app.registry.browser.controlpanel import ControlPanelFormWrapper
from plone.app.registry.browser.controlpanel import RegistryEditForm
from plone.z3cform import layout
from z3c.form import form
from Products.CMFCore.utils import getToolByName
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
import math


class ElasticControlPanelForm(RegistryEditForm):
    form.extends(RegistryEditForm)
    schema = IElasticSettings

    label = u'Elasic Search Settings'

    control_panel_view = '@@elastic-controlpanel'


class ElasticControlPanelFormWrapper(ControlPanelFormWrapper):
    index = ViewPageTemplateFile('controlpanel_layout.pt')

    def __init__(self, *args, **kwargs):
        super(ElasticControlPanelFormWrapper, self).__init__(*args, **kwargs)
        self.portal_catalog = getToolByName(self.context, 'portal_catalog')
        self.es = ElasticSearchCatalog(self.portal_catalog)

    @property
    def connection_status(self):
        return self.es.connection.ping() and self.es.connection.cluster.health()['status'] in ('green', 'yellow')

    @property
    def es_info(self):
        try:
            info = self.es.connection.info()
            stats = self.es.connection.indices.stats(
                index=self.es.real_index_name)['indices'][self.es.real_index_name]['total']

            return [
                ('Cluster Name', info.get('name')),
                ('Elastic Search Version', info['version']['number']),
                ('Number of docs', stats['docs']['count']),
                ('Deleted docs', stats['docs']['deleted']),
                ('Size', str(int(math.ceil(
                    stats['store']['size_in_bytes'] / 1024.0 / 1024.0))) + 'MB')
            ]
        except Exception:
            return []

    @property
    def active(self):
        return self.es.get_setting('enabled')

ElasticControlPanelView = layout.wrap_form(ElasticControlPanelForm,
                                           ElasticControlPanelFormWrapper)
