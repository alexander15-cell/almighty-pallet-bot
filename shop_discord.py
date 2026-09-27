"""Staged #website_shop form and approval controls; no login or registration.

The guarded merged runtime supplies its existing connection, exact application
identity, scoped store, and a resolver of current inventory/photos. Importing
this module does not read credentials, connect, post, or publish anything.
"""
from typing import Awaitable, Callable
import base64
from io import BytesIO
import discord

from shop_approval import Actor, ShopApprovalError, missing_fields
from shop_content import ShopContentError, verify_content
from shop_values import ShopValidationError, canonical_ebay_url, parse_price_cents

ResolveContent = Callable[[str], Awaitable[dict]]

def dollars(cents):
    return f'${cents // 100}.{cents % 100:02d}' if cents is not None else 'Needed'

def actor_for(interaction, store, application_id):
    user = getattr(interaction, 'user', None)
    if (str(getattr(interaction, 'application_id', '')) != application_id
            or user is None or getattr(user, 'bot', True)
            or str(getattr(interaction, 'guild_id', '')) != store.guild_id
            or str(getattr(interaction, 'channel_id', '')) != store.shop_channel_id):
        raise ShopContentError('invalid_public_content')
    message = getattr(interaction, 'message', None)
    if message is not None:
        author = getattr(message, 'author', None)
        if not author or str(author.id) != application_id or not author.bot:
            raise ShopContentError('invalid_public_content')
    return Actor(store.guild_id, store.shop_channel_id, str(user.id))

def review_embed(review, title, content=None):
    state_labels = {'draft': 'Needs approval', 'approved': 'Approved — waiting for website',
                    'published': 'Published on website', 'held': 'On hold — not ready to publish',
                    'sold': 'Sold', 'withdrawn': 'Removed from website queue'}
    embed = discord.Embed(title=title[:80], description=state_labels.get(review.state, 'Needs review'), color=0x264D3D)
    if content is not None:
        embed.description += '\n\n' + content['description']
        condition = {'new': 'New', 'open_box': 'Open box', 'used': 'Used'}[content['condition']]
        embed.add_field(name='Condition', value=condition + '\n' + content['condition_notes'], inline=False)
    embed.add_field(name='Selling price', value=dollars(review.price_cents), inline=True)
    embed.add_field(name='Available', value=str(review.quantity) if review.quantity else 'Needs stock count', inline=True)
    embed.add_field(name='eBay item link', value=review.ebay_url or 'Needed', inline=False)
    embed.add_field(name='Next step', value='Enter the price and full eBay item link, check the item, then approve it for the website.', inline=False)
    if review.published_revision is not None and review.state == 'draft':
        embed.add_field(name='Changes not live yet', value='The last approved version stays live until these changes are approved.', inline=False)
    # Deliberately not a WEBSITE/1 card: an ordinary review post is not publishable.
    embed.set_footer(text=f'Website review · Item {review.item_id} · Version {review.revision}')
    return embed

async def tell(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

def friendly_error(error):
    code = getattr(error, 'code', '')
    if code in {'invalid_price', 'price_out_of_range'}:
        return 'Enter a price such as 24.99. It must be greater than zero, with no more than two decimal places.'
    if code in {'invalid_ebay_url', 'unsupported_ebay_variation'}:
        return 'Paste the full eBay item link, such as https://www.ebay.com/itm/123456789012. Store, search, shortened, and variation links are not supported.'
    if code == 'item_not_available':
        return 'This item is sold, on hold, or no longer ready. Nothing was approved.'
    if code in {'reviewed_content_changed', 'shop_content_digest_mismatch', 'shop_revision_conflict'}:
        return 'This item changed. Use its newest review card and check the details again.'
    if code in {'shop_operator_not_authorized', 'shop_scope_mismatch'}:
        return 'This action is only for approved staff in #website_shop.'
    if code == 'shop_approval_fields_missing':
        return 'Add the selling price and full eBay item link, and finish the item details before approving.'
    return 'This review could not be completed. Check your access and use the newest review card. Nothing was newly approved.'

class ShopReviewView(discord.ui.View):
    def __init__(self, *, store, review, title, resolver: ResolveContent, application_id: str):
        super().__init__(timeout=None)
        self.store, self.review, self.title = store, review, title
        self.resolver, self.application_id = resolver, application_id
        self.details = discord.ui.Button(label='Enter price and eBay link', style=discord.ButtonStyle.secondary,
            custom_id=f'fgshop:details:{review.item_id}:{review.revision}', disabled=review.state not in {'draft', 'approved', 'published'})
        self.approve = discord.ui.Button(label='Approve for website', style=discord.ButtonStyle.success,
            custom_id=f'fgshop:approve:{review.item_id}:{review.revision}',
            disabled=review.state != 'draft' or bool(missing_fields(review)))
        self.details.callback = self.open_details
        self.approve.callback = self.approve_item
        self.refresh = discord.ui.Button(label='Show latest review', style=discord.ButtonStyle.secondary,
            custom_id=f'fgshop:refresh:{review.item_id}:{review.revision}')
        self.refresh.callback = self.refresh_review
        self.add_item(self.details)
        self.add_item(self.approve)
        self.add_item(self.refresh)

    def current(self, interaction, *, for_edit=False):
        actor = actor_for(interaction, self.store, self.application_id)
        current = self.store.get_review(actor, self.review.item_id)
        permitted_states = {'draft', 'approved', 'published'} if for_edit else {'draft'}
        if current.revision != self.review.revision or current.state not in permitted_states:
            raise ShopContentError('reviewed_content_changed')
        return actor, current

    async def open_details(self, interaction):
        try:
            self.current(interaction, for_edit=True)
            await interaction.response.send_modal(ShopDetailsModal(self, str(interaction.user.id)))
        except (ShopApprovalError, ShopContentError, ShopValidationError) as error:
            await tell(interaction, friendly_error(error))

    async def approve_item(self, interaction):
        try:
            actor, review = self.current(interaction)
            await interaction.response.defer(ephemeral=True, thinking=True)
            verified = verify_content(await self.resolver(review.product_ref))
            if verified.product_ref != review.product_ref or verified.quantity != review.quantity or verified.digest != review.content_digest:
                raise ShopContentError('reviewed_content_changed')
            # The transaction rechecks state/revision after the asynchronous read.
            job = self.store.approve(actor, review.item_id, expected_revision=review.revision,
                                     request_id=f'discord-{interaction.id}', validated_content_digest=verified.digest)
        except (ShopApprovalError, ShopContentError, ShopValidationError) as error:
            await tell(interaction, friendly_error(error))
            return
        except Exception:
            await tell(interaction, 'The current item could not be checked. Please try again; nothing was newly approved.')
            return
        # Delivery of this notice is separate from the committed approval. A
        # failed Discord response must not pretend the durable job was undone.
        try:
            latest = self.store.get_review(actor, review.item_id)
            if job.state not in {'queued', 'claimed', 'published'} or latest.revision != job.revision or latest.state not in {'approved', 'published'}:
                await tell(interaction, 'The review changed or was cancelled. Use Show latest review before trying again.')
            elif job.state == 'published':
                await tell(interaction, 'This version has already been confirmed by the website.')
            else:
                await tell(interaction, 'Approved and queued for the website. It is not live until the website confirms the update.')
        except Exception:
            pass

    async def refresh_review(self, interaction):
        try:
            actor = actor_for(interaction, self.store, self.application_id)
            current = self.store.get_review(actor, self.review.item_id)
            await interaction.response.defer(ephemeral=True, thinking=True)
            title = self.title
            if current.state in {'draft', 'approved', 'published'}:
                verified = verify_content(await self.resolver(current.product_ref))
                if verified.product_ref != current.product_ref:
                    raise ShopContentError('reviewed_content_changed')
                title = verified.title
                if verified.digest != current.content_digest or verified.quantity != current.quantity:
                    current = self.store.edit(actor, current.item_id, expected_revision=current.revision,
                        request_id=f'discord-{interaction.id}', content_digest=verified.digest, quantity=verified.quantity)
            await post_review(interaction.channel, store=self.store, review=current, title=title,
                              resolver=self.resolver, application_id=self.application_id)
            await tell(interaction, 'The latest review is posted in #website_shop. No new approval was given.')
        except (ShopApprovalError, ShopContentError, ShopValidationError) as error:
            await tell(interaction, friendly_error(error))
        except Exception:
            await tell(interaction, 'The latest review could not be posted. Nothing was newly approved; try Show latest review again.')

class ShopDetailsModal(discord.ui.Modal):
    def __init__(self, view, owner_id):
        super().__init__(title='Website price and eBay link', timeout=300,
                         custom_id=f'fgshop:form:{view.review.item_id}:{view.review.revision}')
        self.review_view, self.owner_id = view, owner_id
        cents = view.review.price_cents
        self.price = discord.ui.TextInput(label='Selling price ($)', placeholder='24.99',
            default=None if cents is None else f'{cents // 100}.{cents % 100:02d}', max_length=128, required=True)
        self.ebay = discord.ui.TextInput(label='Full eBay item link', placeholder='https://www.ebay.com/itm/...',
            default=view.review.ebay_url, max_length=2048, required=True)
        self.add_item(self.price)
        self.add_item(self.ebay)

    async def on_submit(self, interaction):
        view = self.review_view
        try:
            if str(interaction.user.id) != self.owner_id:
                raise ShopContentError('invalid_public_content')
            actor, current = view.current(interaction, for_edit=True)
            cents, url = parse_price_cents(str(self.price)), canonical_ebay_url(str(self.ebay))
            await interaction.response.defer(ephemeral=True, thinking=True)
            verified = verify_content(await view.resolver(current.product_ref))
            if verified.product_ref != current.product_ref or verified.quantity != current.quantity:
                raise ShopContentError('reviewed_content_changed')
            changed = view.store.edit(actor, current.item_id, expected_revision=current.revision,
                request_id=f'discord-{interaction.id}', price_cents=cents, ebay_url=url, content_digest=verified.digest)
            # A new message preserves every prior card. Old buttons reject its
            # revision. A failed send leaves a draft, never an automatic publish.
            await post_review(interaction.channel, store=view.store, review=changed, title=verified.title,
                              resolver=view.resolver, application_id=view.application_id)
            await tell(interaction, 'Saved. Check the new review card in #website_shop, then click Approve for website.')
        except (ShopApprovalError, ShopContentError, ShopValidationError) as error:
            await tell(interaction, friendly_error(error))
        except Exception:
            await tell(interaction, 'The review card could not be posted. No new approval was given. Click Show latest review on the earlier card to recover the saved draft.')

async def post_review(channel, *, store, review, title, resolver, application_id):
    member = getattr(getattr(channel, 'guild', None), 'me', None)
    if (str(getattr(channel, 'id', '')) != store.shop_channel_id
            or str(getattr(getattr(channel, 'guild', None), 'id', '')) != store.guild_id
            or str(getattr(member, 'id', '')) != application_id or not getattr(member, 'bot', False)):
        raise ShopContentError('invalid_public_content')
    view = ShopReviewView(store=store, review=review, title=title, resolver=resolver, application_id=application_id)
    if review.state in {'held', 'sold', 'withdrawn'}:
        message = await channel.send(embed=review_embed(review, title), view=view, allowed_mentions=discord.AllowedMentions.none())
        recorder = getattr(store, 'record_review_message', None)
        if recorder is not None:
            recorder(review, title, message.id)
        return message
    content = await resolver(review.product_ref)
    verified = verify_content(content)
    if verified.product_ref != review.product_ref or verified.digest != review.content_digest or verified.quantity != review.quantity:
        raise ShopContentError('reviewed_content_changed')
    embed = review_embed(review, verified.title, content)
    files, embeds = [], [embed]
    try:
        for index, photo in enumerate(content['photos']):
            extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[photo['mimeType']]
            filename = f'shop-photo-{index + 1}.{extension}'
            files.append(discord.File(BytesIO(base64.b64decode(photo['base64'], validate=True)), filename=filename))
            if index == 0:
                embed.set_image(url='attachment://' + filename)
            else:
                embeds.append(discord.Embed().set_image(url='attachment://' + filename))
        message = await channel.send(embeds=embeds, files=files, view=view, allowed_mentions=discord.AllowedMentions.none())
        recorder = getattr(store, 'record_review_message', None)
        if recorder is not None:
            recorder(review, verified.title, message.id)
        return message
    finally:
        for file in files:
            file.close()
