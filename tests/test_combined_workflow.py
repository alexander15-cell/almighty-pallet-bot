"""Merged intake -> Discord form -> approval -> website ACK -> sold, offline."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from combined_delivery import DeliveryService
from publisher.journal import Journal
from publisher.transport import Delivery
from shop_discord import ShopDetailsModal
from test_combined_intake import case, APP, GUILD, SHOP, USER, URL
from test_shop_discord import Response

class Website:
    website='https://offline-fixture.example'
    def __init__(self): self.calls=[]
    def post(self, suffix, body):
        value=json.loads(body)
        self.calls.append((suffix,value))
        if suffix=='heartbeat': return Delivery(True,value={'ok':True})
        return Delivery(True,value={'ok':True,'outcome':'applied','version':value['version']})
    @property
    def products(self): return [v for suffix,v in self.calls if suffix=='sync']

def event(case, number):
    return SimpleNamespace(id=number, application_id=int(APP), guild_id=int(GUILD), channel_id=int(SHOP),
        user=SimpleNamespace(id=int(USER),bot=False),
        message=SimpleNamespace(author=SimpleNamespace(id=int(APP),bot=True)),
        channel=case.channel, response=Response(), followup=SimpleNamespace(send=AsyncMock()))

def test_full_merged_workflow(case,tmp_path,monkeypatch):
    async def scenario():
        monkeypatch.setattr('socket.socket.connect',lambda *a,**k: (_ for _ in ()).throw(AssertionError('No network allowed')))
        website=Website()
        journal=Journal(tmp_path/'delivery.sqlite','a'*32,GUILD,website.website,APP,
                        case.settings.channels['listed'],case.settings.channels['sold'])
        try:
            service=DeliveryService(case.store,journal,website,case.adapter.resolve_content,case.adapter.lifecycle,enabled=True)
            original_message=case.db.get_item(case.item_id)['current_message_id']
            await case.adapter.reconcile()
            await service.tick()
            assert not website.products
            view=case.channel.sent[-1][1]['view']
            assert view.approve.disabled
            modal=ShopDetailsModal(view,USER)
            modal.price._value='39.95'
            modal.ebay._value=URL+'?tracking=example'
            await modal.on_submit(event(case,2001))
            assert case.store.system_get_review(str(case.item_id)).state=='draft'
            await service.tick()
            assert not website.products
            review_view=case.channel.sent[-1][1]['view']
            assert not review_view.approve.disabled
            await review_view.approve_item(event(case,2002))
            assert case.store.system_get_review(str(case.item_id)).state=='approved'
            assert not website.products
            result=await service.tick()
            assert result['sent']==result['acknowledged']==1
            value=website.products[-1]
            assert value['listing']['priceCents']==3995
            assert value['listing']['ebayItemId']=='123456789012'
            assert value['listing']['quantity']==1
            assert len(value['listing']['photos'])==1
            assert 'PRIVATE' not in json.dumps(value)
            assert case.store.system_get_review(str(case.item_id)).state=='published'
            await case.adapter.reconcile()
            assert 'Published on website' in case.channel.sent[-1][1]['embeds'][0].description
            assert case.db.get_item(case.item_id)['current_message_id']==original_message
            # Restart the delivery service against the same durable state.
            restarted=DeliveryService(case.store,journal,website,case.adapter.resolve_content,case.adapter.lifecycle,enabled=True)
            await restarted.tick()
            assert len(website.products)==1
            case.db.update_status(case.item_id,'sold',actor_id=int(USER),new_message_id=500000000000000009)
            case.photo.unlink()  # Removal must not depend on retaining photos.
            await case.adapter.reconcile()
            result=await restarted.tick()
            assert result['withdrawn']==1
            assert website.products[-1]['status']=='sold'
            assert website.products[-1]['listing'] is None
            assert website.products[-1]['version']>value['version']
            assert case.store.system_get_review(str(case.item_id)).state=='sold'
            await restarted.tick()
            assert len(website.products)==2
            assert case.store.system_get_review(str(case.legacy)) is None
        finally: journal.close()
    asyncio.run(scenario())
