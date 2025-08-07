from typing import Dict, Callable, Awaitable, Optional
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from datetime import datetime, timedelta
import sqlite3
import pytz
import os
import asyncio
import json
import sys
import time

from utils.message_utils import MessageError, PostType
from database.manager import DatabaseManager
from utils.validators import InputValidator
from conversation_states import MAIN_MENU, SCHEDULE_SELECT_CHANNEL, SCHEDULE_SEND, SETTINGS, WAITING_CHANNEL_SELECTION, WAITING_CHANNEL_INFO, WAITING_PUBLICATION_CONTENT, AUTO_DESTRUCTION, WAITING_TAG_INPUT
from utils.error_handler import handle_error
from utils.scheduler import SchedulerManager
from utils.scheduler_utils import send_scheduled_file
from config import settings


logger = logging.getLogger(__name__)

# Variable globale pour le scheduler manager
_global_scheduler_manager = None

# Fonction pour définir le scheduler manager global
def set_global_scheduler_manager(scheduler_manager):
    """Définit le scheduler manager global"""
    global _global_scheduler_manager
    _global_scheduler_manager = scheduler_manager
    logger.info("✅ Scheduler manager global défini")

# Fonction pour récupérer le gestionnaire de scheduler
def get_scheduler_manager():
    """Récupère l'instance du gestionnaire de scheduler"""
    global _global_scheduler_manager
    
    try:
        # Priorité 1 : Utiliser le scheduler global s'il est défini
        if _global_scheduler_manager is not None:
            logger.info("✅ Scheduler manager récupéré depuis la variable globale")
            return _global_scheduler_manager
        
        # Priorité 2 : Essayer de récupérer depuis le module bot
        try:
            import sys
            if 'bot' in sys.modules:
                bot_module = sys.modules['bot']
                if hasattr(bot_module, 'application') and hasattr(bot_module.application, 'scheduler_manager'):
                    current_app = bot_module.application
                    logger.info("✅ Scheduler manager récupéré depuis le module bot")
                    return current_app.scheduler_manager
        except Exception as e:
            logger.debug(f"Impossible de récupérer depuis le module bot: {e}")
        
        # Priorité 3 : Fallback - créer une instance temporaire mais avec warning
        logger.warning("⚠️ Scheduler manager non trouvé - création d'une instance temporaire")
        logger.warning("⚠️ Les tâches planifiées ne fonctionneront pas correctement !")
        return SchedulerManager("UTC")
    except Exception as e:
        logger.error(f"Erreur lors de la récupération du scheduler manager: {e}")
        return None

# Fonction utilitaire pour éviter les erreurs "Message not modified" dans les callbacks
async def safe_edit_callback_message(query, text, reply_markup=None, parse_mode=None):
    """
    Édite un message de callback de manière sûre en évitant l'erreur "Message not modified"
    Optimisée pour les CallbackQuery dans ce fichier
    """
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            logger.debug("Message identique, pas d'édition nécessaire")
            return
        else:
            logger.error(f"Erreur lors de l'édition du message: {e}")
            raise

# Fonction utilitaire pour normaliser les noms de canaux
def normalize_channel_username(channel_username):
    """
    Normalise le nom d'utilisateur d'un canal en enlevant @ si présent
    """
    if not channel_username:
        return None
    return channel_username.lstrip('@') if isinstance(channel_username, str) else None

def schedule_auto_destruction(context, chat_id, message_id, auto_destruction_time):
    """Programme l'auto-destruction d'un message"""
    try:
        def delete_message_callback(context_job):
            try:
                # Utiliser le bot du job context
                bot = context_job.context.bot
                chat_id = context_job.context.chat_id
                message_id = context_job.context.message_id
                
                # Supprimer le message
                bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id
                )
                logger.info(f"🗑️ Message auto-supprimé après {auto_destruction_time}s")
            except Exception as e:
                logger.warning(f"Erreur suppression auto: {e}")
        
        # Créer un contexte pour le job
        from datetime import timedelta
        job_context = type('JobContext', (), {
            'bot': context.bot,
            'chat_id': chat_id,
            'message_id': message_id
        })()
        
        # Programmer la suppression
        if hasattr(context, 'application') and hasattr(context.application, 'job_queue'):
            context.application.job_queue.run_once(
                delete_message_callback,
                when=timedelta(seconds=auto_destruction_time),
                name=f"auto_delete_{message_id}",
                context=job_context
            )
            logger.info(f"⏰ Auto-destruction programmée dans {auto_destruction_time}s")
            return True
        else:
            logger.warning("Job queue non disponible")
            return False
    except Exception as e:
        logger.warning(f"Impossible de programmer l'auto-destruction: {e}")
        return False

# Définition des types pour les gestionnaires
HandlerType = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

class CallbackError(Exception):
    """Exception pour les erreurs de callback"""
    pass

# Mapping des actions vers les gestionnaires
CALLBACK_HANDLERS: Dict[str, HandlerType] = {
    "main_menu": "start",
    "create_publication": "create_publication",
    "planifier_post": "planifier_post",
    "modifier_heure": "handle_edit_time",
    "envoyer_maintenant": "handle_send_now",
    "annuler_publication": "handle_cancel_post",
    "retour": "planifier_post",
    "preview": "handle_preview",
    "settings": "handle_settings",
    "timezone": "handle_timezone_setup",
    "schedule_send": "schedule_send"
}

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gère les callbacks de manière centralisée.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Raises:
        CallbackError: Si le callback est invalide ou non géré
    """
    # Récupère le callback Telegram standard
    query = update.callback_query
    user_id = update.effective_user.id
    if not query or not query.data:
        logger.warning("Callback sans données reçu")
        return

    # ✅ LOG DE DÉBOGAGE POUR LES CALLBACKS
    logger.info(f"🔍 Callback reçu: {query.data}")
    logger.info(f"🔍 Utilisateur: {user_id}")
    logger.info(f"🔍 Chat: {query.message.chat_id if query.message else 'N/A'}")

    try:
        # Récupération du callback data complet
        callback_data = query.data
        logger.info(f"🔍 Callback reçu: {callback_data}")
        await query.answer()
        
        # Log détaillé pour déboguer
        logger.info(f"🔍 Callback complet: '{callback_data}'")
        logger.info(f"🔍 Type: {type(callback_data)}")
        logger.info(f"🔍 Longueur: {len(callback_data)}")

        # Cas spécifiques pour les callbacks
        if callback_data == "main_menu":
            # Retour au menu principal
            keyboard = [
                [InlineKeyboardButton("📝 Nouvelle publication", callback_data="create_publication")],
                [InlineKeyboardButton("📅 Publications planifiées", callback_data="planifier_post")],
                [InlineKeyboardButton("📊 Statistiques", callback_data="channel_stats")],
                [InlineKeyboardButton("⚙️ Paramètres", callback_data="settings")]
            ]
            
            await safe_edit_callback_message(
                query,
                "Menu principal :",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return MAIN_MENU
            
        elif callback_data == "create_publication":
            # Aller directement à la sélection des canaux pour créer une publication
            return await handle_create_publication(update, context)
            
        elif callback_data == "planifier_post":
            return await planifier_post(update, context)
            
        elif callback_data == "schedule_send":
            return await schedule_send(update, context)
            
        elif callback_data == "send_now":
            # Bouton "Maintenant" - utilise maintenant la vraie fonction send_post_now
            logger.info("🔥 DEBUG: Callback send_now reçu, appel de send_post_now")
            return await send_post_now(update, context)
            
        elif callback_data == "auto_destruction":
            # Bouton "Régler temps d'auto destruction" - FONCTIONNALITÉ RÉELLE
            from utils.message_templates import MessageTemplates
            
            keyboard = [
                [InlineKeyboardButton("5 minutes", callback_data="auto_dest_300")],
                [InlineKeyboardButton("30 minutes", callback_data="auto_dest_1800")],
                [InlineKeyboardButton("1 heure", callback_data="auto_dest_3600")],
                [InlineKeyboardButton("6 heures", callback_data="auto_dest_21600")],
                [InlineKeyboardButton("24 heures", callback_data="auto_dest_86400")],
                [InlineKeyboardButton("❌ Désactiver", callback_data="auto_dest_0")],
                [InlineKeyboardButton("↩️ Retour", callback_data="main_menu")]
            ]
            
            await safe_edit_callback_message(
                query,
                MessageTemplates.get_auto_destruction_message(),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return AUTO_DESTRUCTION
            
        # Gestion des choix d'auto-destruction
        elif callback_data.startswith("auto_dest_"):
            seconds = int(callback_data.split("_")[-1])
            
            if seconds == 0:
                # Désactiver l'auto-destruction
                context.user_data.pop('auto_destruction_time', None)
                await safe_edit_callback_message(
                    query,
                    "✅ **Auto-destruction désactivée**\n\n"
                    "Vos messages ne seront pas supprimés automatiquement.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Retour au menu d'envoi", callback_data="main_menu")
                    ]]),
                    parse_mode='Markdown'
                )
            else:
                # Enregistrer le temps d'auto-destruction
                context.user_data['auto_destruction_time'] = seconds
                
                # Convertir en format lisible
                if seconds < 3600:
                    time_str = f"{seconds // 60} minute(s)"
                elif seconds < 86400:
                    time_str = f"{seconds // 3600} heure(s)"
                else:
                    time_str = f"{seconds // 86400} jour(s)"
                
                await safe_edit_callback_message(
                    query,
                    f"✅ **Auto-destruction configurée**\n\n"
                    f"⏰ Durée : {time_str}\n\n"
                    f"Vos prochains messages se supprimeront automatiquement après {time_str}.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Retour au menu d'envoi", callback_data="main_menu")
                    ]]),
                    parse_mode='Markdown'
                )
            
            return MAIN_MENU
            
        elif callback_data == "schedule_today" or callback_data == "schedule_tomorrow":
            # Stocker le jour sélectionné et rediriger vers handle_schedule_time
            context.user_data['schedule_day'] = 'today' if callback_data == "schedule_today" else 'tomorrow'
            jour = "Aujourd'hui" if context.user_data['schedule_day'] == 'today' else "Demain"
            
            logger.info(f"📅 Jour sélectionné: {jour}")

            # Mise à jour du message pour indiquer que l'heure est attendue
            await query.edit_message_text(
                f"✅ Jour sélectionné : {jour}.\n\n"
                "Envoyez-moi maintenant l'heure au format :\n"
                "   • '15:30' ou '1530' (24h)\n"
                "   • '6' (06:00)\n"
                "   • '5 3' (05:03)\n\n"
                "⏰ En attente de l'heure...",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="schedule_send")
                ]])
            )
            return SCHEDULE_SEND
            
        elif callback_data == "modifier_heure":
            return await handle_edit_time(update, context)
            
        elif callback_data == "envoyer_maintenant":
            # Unification : utilise la même fonction send_post_now
            return await send_post_now(update, context)
            
        elif callback_data == "annuler_publication":
            return await handle_cancel_post(update, context)
            
        elif callback_data == "confirm_cancel":
            return await handle_confirm_cancel(update, context)
            
        elif callback_data == "retour":
            return await planifier_post(update, context)
            
        elif callback_data == "settings":
            # Redirection vers le menu des paramètres personnalisés
            return await custom_settings_menu(update, context)
            
        # Gestion des canaux
        elif callback_data == "manage_channels":
            return await manage_channels_menu(update, context)
            
        elif callback_data == "timezone_settings":
            return await handle_timezone_settings(update, context)
            
        elif callback_data.startswith("set_timezone_"):
            timezone_code = callback_data.replace("set_timezone_", "")
            return await handle_set_timezone(update, context, timezone_code)
            
        elif callback_data == "manual_timezone":
            return await handle_manual_timezone(update, context)
            
        elif callback_data == "add_channel":
            return await add_channel_prompt(update, context)
            
        elif callback_data == "use_default_channel":
            return await use_default_channel(update, context)
            
        elif callback_data.startswith("select_channel_"):
            channel_username = callback_data.replace("select_channel_", "")
            return await select_channel(update, context, channel_username)
            
        elif callback_data.startswith("channel_"):
            channel_username = callback_data.replace("channel_", "")
            return await show_channel_options(update, context, channel_username)
            
        elif callback_data.startswith("custom_channel_"):
            channel_username = callback_data.replace("custom_channel_", "")
            return await custom_channel_settings(update, context, channel_username)
            
        elif callback_data == "custom_settings":
            return await custom_settings_menu(update, context)
            
        elif callback_data == "thumbnail_menu":
            # Gestion du menu thumbnail
            from .thumbnail_handler import handle_thumbnail_functions
            return await handle_thumbnail_functions(update, context)
            
        elif callback_data == "view_thumbnail":
            # Afficher le thumbnail actuel
            from .thumbnail_handler import handle_view_thumbnail
            return await handle_view_thumbnail(update, context)
            
        elif callback_data == "delete_thumbnail":
            # Supprimer le thumbnail
            from .thumbnail_handler import handle_delete_thumbnail
            return await handle_delete_thumbnail(update, context)
            
        elif callback_data == "add_thumbnail":
            # Ajouter un thumbnail
            from .thumbnail_handler import handle_add_thumbnail
            return await handle_add_thumbnail(update, context)
            
        elif callback_data == "confirm_large_thumbnail":
            # Confirmer l'utilisation d'un thumbnail volumineux
            temp_thumbnail = context.user_data.get('temp_thumbnail')
            if temp_thumbnail:
                # Utiliser le thumbnail temporaire même s'il est volumineux
                selected_channel = context.user_data.get('selected_channel', {})
                channel_username = selected_channel.get('username')
                user_id = update.effective_user.id
                
                if channel_username:
                    clean_username = normalize_channel_username(channel_username)
                    db_manager = DatabaseManager()
                    try:
                        success = db_manager.save_thumbnail(clean_username, user_id, temp_thumbnail)
                        if success:
                            context.user_data['waiting_for_channel_thumbnail'] = False
                            context.user_data.pop('temp_thumbnail', None)
                            
                            await query.edit_message_text(
                                f"✅ Thumbnail volumineux enregistré pour @{clean_username}!",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("↩️ Retour", callback_data=f"custom_channel_{clean_username}")
                                ]])
                            )
                            return SETTINGS
                    except Exception as e:
                        logger.error(f"Erreur lors de l'enregistrement du thumbnail volumineux: {e}")
                        
            await query.edit_message_text(
                "❌ Erreur lors de l'enregistrement du thumbnail.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="thumbnail_menu")
                ]])
            )
            return SETTINGS
            
        elif callback_data.startswith("delete_channel_"):
            channel_id = callback_data.replace("delete_channel_", "")
            return await delete_channel(update, context, channel_id)
            
        elif callback_data.startswith("confirm_delete_channel_"):
            channel_id = callback_data.replace("confirm_delete_channel_", "")
            return await confirm_delete_channel(update, context, channel_id)
            
        elif callback_data.startswith("edit_file_"):
            post_index = callback_data.replace("edit_file_", "")
            return await show_edit_file_menu(update, context, int(post_index))
            
        elif callback_data == "preview_all":
            await query.edit_message_text(
                "📋 **Aperçu général**\n\n"
                "Cette fonctionnalité sera bientôt disponible.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU
            
        elif callback_data == "delete_all_posts":
            # Supprimer tous les posts mais garder le canal sélectionné
            if 'posts' in context.user_data:
                context.user_data['posts'] = []
            # Ne pas supprimer le canal : context.user_data.pop('selected_channel', None)
            
            await query.edit_message_text(
                "🗑️ **Tous les posts supprimés**\n\n"
                "📤 Envoyez maintenant vos nouveaux fichiers :"
            )
            return WAITING_PUBLICATION_CONTENT
            
        elif callback_data.startswith("rename_post_"):
            post_index = callback_data.replace("rename_post_", "")
            return await handle_rename_post(update, context, int(post_index))
            
        elif callback_data.startswith("edit_file_"):
            post_index = callback_data.replace("edit_file_", "")
            return await show_edit_file_menu(update, context, int(post_index))
            
        elif callback_data.startswith("add_thumbnail_"):
            post_index = callback_data.replace("add_thumbnail_", "")
            return await handle_add_thumbnail_to_post_callback(update, context, int(post_index))
            
        elif callback_data.startswith("add_thumb_"):
            # Reproduit la logique du renambot: afficher MEDIA INFO puis attendre le nouveau nom
            try:
                parts = callback_data.split('_')
                post_index = int(parts[-1])
            except Exception:
                await query.answer("❌ Erreur de format", show_alert=True)
                return MAIN_MENU

            # Valider le post
            if 'posts' not in context.user_data or post_index >= len(context.user_data['posts']):
                await query.answer("❌ Post introuvable", show_alert=True)
                return MAIN_MENU

            post = context.user_data['posts'][post_index]
            file_name = post.get('filename', 'unnamed_file')
            file_size = post.get('file_size', 0)
            extension = os.path.splitext(file_name)[1] or "Unknown"
            mime_type = post.get('mime_type', 'Unknown')
            dc_id = post.get('dc_id', 'N/A')

            # Vérifier qu'un thumbnail est défini pour le canal sélectionné
            channel_username = post.get('channel', context.user_data.get('selected_channel', {}).get('username'))
            clean_username = normalize_channel_username(channel_username)
            db_manager = DatabaseManager()
            thumbnail_data = db_manager.get_thumbnail(clean_username, update.effective_user.id)
            if not thumbnail_data:
                await query.answer("❌ Aucun thumbnail défini pour ce canal", show_alert=True)
                return MAIN_MENU

            info_card = (
                "📁 <b>MEDIA INFO</b>\n\n"
                f"📁 <b>FILE NAME:</b> <code>{file_name}</code>\n"
                f"🧩 <b>EXTENSION:</b> <code>{extension}</code>\n"
                f"📦 <b>FILE SIZE:</b> {file_size}\n"
                f"🪄 <b>MIME TYPE:</b> {mime_type}\n"
                f"🧭 <b>DC ID:</b> {dc_id}\n\n"
                "<b>PLEASE ENTER THE NEW FILENAME WITH EXTENSION AND REPLY THIS MESSAGE.</b>"
            )

            await safe_edit_callback_message(query, info_card, parse_mode='HTML')

            # Stocker l'action et l'index
            context.user_data['awaiting_thumb_rename'] = True
            context.user_data['current_post_index'] = post_index
            return WAITING_THUMBNAIL_RENAME_INPUT
            
        elif callback_data.startswith("add_reactions_"):
            # Gestion de l'ajout de réactions
            try:
                post_index = int(callback_data.split('_')[-1])
            except Exception as e:
                logger.error(f"Erreur parsing add_reactions_: {callback_data} - {e}")
                await query.answer("❌ Erreur de format")
                return MAIN_MENU
            from .reaction_functions import add_reactions_to_post
            return await add_reactions_to_post(update, context)
            
        elif callback_data.startswith("add_url_button_"):
            # Gestion de l'ajout de boutons URL
            try:
                post_index = int(callback_data.split('_')[-1])
            except Exception as e:
                logger.error(f"Erreur parsing add_url_button_: {callback_data} - {e}")
                await query.answer("❌ Erreur de format")
                return MAIN_MENU
            from .reaction_functions import add_url_button_to_post
            return await add_url_button_to_post(update, context)
            
        elif callback_data.startswith("remove_reactions_"):
            # Gestion de la suppression de réactions
            try:
                post_index = int(callback_data.split('_')[-1])
            except Exception as e:
                logger.error(f"Erreur parsing remove_reactions_: {callback_data} - {e}")
                await query.answer("❌ Erreur de format")
                return MAIN_MENU
            from .reaction_functions import remove_reactions
            return await remove_reactions(update, context)
            
        elif callback_data.startswith("remove_url_buttons_"):
            # Gestion de la suppression de boutons URL
            try:
                post_index = int(callback_data.split('_')[-1])
            except Exception as e:
                logger.error(f"Erreur parsing remove_url_buttons_: {callback_data} - {e}")
                await query.answer("❌ Erreur de format")
                return MAIN_MENU
            from .reaction_functions import remove_url_buttons
            return await remove_url_buttons(update, context)
            
        elif callback_data.startswith("delete_post_"):
            # Gestion de la suppression de posts
            try:
                post_index = int(callback_data.split('_')[-1])
            except Exception as e:
                logger.error(f"Erreur parsing delete_post_: {callback_data} - {e}")
                await query.answer("❌ Erreur de format")
                return MAIN_MENU
            return await handle_delete_post(update, context, post_index)
            
        elif callback_data.startswith("edit_tag_"):
            # Gestion de l'ajout/modification de hashtags
            channel_username = callback_data.replace("edit_tag_", "")
            return await handle_edit_tag(update, context, channel_username)

        elif callback_data.startswith("show_post_"):
            # Gestion de l'affichage des posts planifiés
            return await show_scheduled_post(update, context)

        # ✅ GESTIONNAIRE POUR LES RÉACTIONS DANS LE CANAL
        elif callback_data.startswith("reaction_"):
            try:
                logger.info(f"🎯 GESTIONNAIRE RÉACTIONS ACTIVÉ - Callback: {callback_data}")
                
                # Format: reaction_{post_index}_{reaction}
                parts = callback_data.split("_")
                logger.info(f"🎯 Parts du callback: {parts}")
                
                if len(parts) >= 3:
                    post_index = parts[1]
                    reaction = "_".join(parts[2:])  # En cas de réaction avec underscore
                    
                    logger.info(f"⭐ Réaction cliquée: {reaction} pour le post {post_index}")
                    logger.info(f"⭐ Utilisateur: {user_id}")
                    logger.info(f"⭐ Chat: {query.message.chat_id if query.message else 'N/A'}")
                    
                    # Incrémenter le compteur de réactions
                    if 'reaction_counts' not in context.bot_data:
                        context.bot_data['reaction_counts'] = {}
                        logger.info("📊 Initialisation du dictionnaire reaction_counts")
                    
                    reaction_key = f"{post_index}_{reaction}"
                    current_count = context.bot_data['reaction_counts'].get(reaction_key, 0)
                    context.bot_data['reaction_counts'][reaction_key] = current_count + 1
                    
                    # Afficher une notification à l'utilisateur
                    notification_text = f"👍 {reaction} +1"
                    await query.answer(notification_text)
                    logger.info(f"✅ Notification envoyée: {notification_text}")
                    
                    logger.info(f"✅ Réaction {reaction} comptée pour le post {post_index} (total: {current_count + 1})")
                    
                else:
                    logger.warning(f"Format de callback de réaction invalide: {callback_data}")
                    logger.warning(f"Nombre de parts: {len(parts)}")
                    await query.answer("❌ Erreur de format")
                    
            except Exception as e:
                logger.error(f"Erreur lors du traitement de la réaction: {e}")
                logger.exception("🔍 Traceback complet:")
                await query.answer("❌ Erreur lors du traitement")

        # Si le callback n'est pas dans la liste des cas directement gérés
        logger.warning(f"Callback non géré directement : {callback_data}")
        # Protection contre les callbacks malformés
        if '_' in callback_data:
            try:
                # Essayer de parser le callback pour voir s'il contient un index
                parts = callback_data.split('_')
                if parts and parts[-1].isdigit():
                    logger.warning(f"⚠️ Callback avec index numérique non reconnu: {callback_data}")
                    await query.answer("⚠️ Action non implémentée")
                    return MAIN_MENU
            except Exception as e:
                logger.error(f"❌ Erreur parsing callback non reconnu: {callback_data} - {e}")
        
        await query.edit_message_text(
            f"⚠️ Action {callback_data} non implémentée. Retour au menu principal.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]
            )
        )
        return MAIN_MENU

    except Exception as e:
        logger.error(f"Erreur dans handle_callback : {e}")
        await query.edit_message_text(
            "❌ Une erreur est survenue.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]
            )
        )
        return MAIN_MENU


async def handle_edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gère la modification de l'heure d'une publication.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Raises:
        CallbackError: Si la publication n'est pas trouvée
    """
    query = update.callback_query
    try:
        post_id = context.user_data.get('current_post_id')
        if not post_id:
            raise CallbackError("Aucune publication en cours")

        await query.edit_message_text(
            "🕒 Entrez la nouvelle date et heure (format: JJ/MM/AAAA HH:MM):"
        )
        context.user_data['waiting_for_time'] = True

    except CallbackError as e:
        logger.error(f"Erreur de modification d'heure: {str(e)}")
        await query.edit_message_text(f"❌ Erreur: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur inattendue: {str(e)}")
        await query.edit_message_text("❌ Une erreur inattendue s'est produite")


async def handle_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    FONCTION OBSOLÈTE - Remplacée par handle_send_now_unified
    Conservée temporairement pour compatibilité
    """
    logger.warning("⚠️ Utilisation de l'ancienne fonction handle_send_now. Redirection vers handle_send_now_unified")
    return await handle_send_now_unified(update, context)


async def handle_cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gère l'annulation d'une publication.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Raises:
        CallbackError: Si la publication ne peut pas être annulée
    """
    query = update.callback_query
    try:
        if context.user_data.get('confirming_cancel'):
            post_id = context.user_data.get('current_post_id')
            if not post_id:
                raise CallbackError("Aucune publication à annuler")

            db_manager = context.bot_data.get('db_manager')
            if not db_manager or not db_manager.delete_post(post_id):
                raise CallbackError("Impossible d'annuler la publication")

            await query.edit_message_text("✅ Publication annulée")
            context.user_data.pop('confirming_cancel', None)
        else:
            context.user_data['confirming_cancel'] = True
            await query.edit_message_text(
                "⚠️ Êtes-vous sûr de vouloir annuler cette publication ?",
                reply_markup=[[
                    InlineKeyboardButton("Oui", callback_data="annuler_publication"),
                    InlineKeyboardButton("Non", callback_data="retour")
                ]]
            )

    except CallbackError as e:
        logger.error(f"Erreur d'annulation: {str(e)}")
        await query.edit_message_text(f"❌ Erreur: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur inattendue: {str(e)}")
        await query.edit_message_text("❌ Une erreur inattendue s'est produite")


async def handle_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gère l'aperçu d'une publication.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Raises:
        CallbackError: Si la publication n'est pas trouvée
    """
    query = update.callback_query
    try:
        post_data = context.user_data.get('current_post')
        if not post_data:
            raise CallbackError("Aucune publication en cours")

        preview_text = (
            f"📝 Aperçu de la publication:\n\n"
            f"Type: {post_data['type']}\n"
            f"Contenu: {post_data['content'][:100]}...\n"
            f"Légende: {post_data.get('caption', 'Aucune')}\n"
            f"Horaire: {post_data.get('scheduled_time', 'Immédiat')}"
        )

        await query.edit_message_text(preview_text)

    except CallbackError as e:
        logger.error(f"Erreur d'aperçu: {str(e)}")
        await query.edit_message_text(f"❌ Erreur: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur inattendue: {str(e)}")
        await query.edit_message_text("❌ Une erreur inattendue s'est produite")


async def handle_post_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Gère le choix du type de publication.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Returns:
        int: L'état suivant de la conversation

    Raises:
        CallbackError: Si le type de publication est invalide
    """
    query = update.callback_query
    try:
        await query.answer()

        post_type = query.data.split('_')[-1]
        if post_type not in ['text', 'photo', 'video']:
            raise CallbackError("Type de publication invalide")

        context.user_data['post_type'] = post_type

        if post_type == 'text':
            await query.edit_message_text(
                "Entrez le texte de votre publication:"
            )
            return 4  # WAITING_TEXT

        await query.edit_message_text(
            "Envoyez la photo ou la vidéo:"
        )
        return 5  # WAITING_MEDIA

    except CallbackError as e:
        logger.error(f"Erreur de type de publication: {str(e)}")
        await query.edit_message_text(f"❌ Erreur: {str(e)}")
        return 1  # CREATE_PUBLICATION
    except Exception as e:
        logger.error(f"Erreur inattendue: {str(e)}")
        await query.edit_message_text("❌ Une erreur inattendue s'est produite")
        return 1  # CREATE_PUBLICATION


async def handle_schedule_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Gère le choix du type de publication à planifier.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Returns:
        int: L'état suivant de la conversation

    Raises:
        CallbackError: Si le type de publication est invalide
    """
    query = update.callback_query
    try:
        await query.answer()

        post_type = query.data.split('_')[-1]
        if post_type not in ['text', 'photo', 'video']:
            raise CallbackError("Type de publication invalide")

        context.user_data['post_type'] = post_type

        if post_type == 'text':
            await query.edit_message_text(
                "Entrez le texte de votre publication:"
            )
            return 6  # WAITING_SCHEDULE_TEXT

        await query.edit_message_text(
            "Envoyez la photo ou la vidéo:"
        )
        return 7  # WAITING_SCHEDULE_MEDIA

    except CallbackError as e:
        logger.error(f"Erreur de type de publication planifiée: {str(e)}")
        await query.edit_message_text(f"❌ Erreur: {str(e)}")
        return 2  # PLANIFIER_POST
    except Exception as e:
        logger.error(f"Erreur inattendue: {str(e)}")
        await query.edit_message_text("❌ Une erreur inattendue s'est produite")
        return 2  # PLANIFIER_POST


async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Gère les paramètres du bot.

    Args:
        update: L'objet Update de Telegram
        context: Le contexte de la conversation

    Returns:
        int: L'état suivant de la conversation

    Raises:
        CallbackError: Si le type de paramètre est invalide
    """
    query = update.callback_query
    try:
        await query.answer()

        setting_type = query.data.split('_')[-1]
        if setting_type not in ['timezone', 'other']:
            raise CallbackError("Type de paramètre invalide")

        if setting_type == 'timezone':
            await query.edit_message_text(
                "Entrez votre fuseau horaire (ex: Europe/Paris):"
            )
            return 8  # WAITING_TIMEZONE

        await query.edit_message_text(
            "Autres paramètres à venir..."
        )
        return ConversationHandler.END

    except CallbackError as e:
        logger.error(f"Erreur de paramètres: {str(e)}")
        await query.edit_message_text(f"❌ Erreur: {str(e)}")
        return 3  # SETTINGS
    except Exception as e:
        logger.error(f"Erreur inattendue: {str(e)}")
        await query.edit_message_text("❌ Une erreur inattendue s'est produite")
        return 3  # SETTINGS


async def handle_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la planification effective des messages - VERSION FINALE CORRIGÉE"""
    try:
        # Vérifications de base
        if not update.message or not update.message.text:
            return SCHEDULE_SEND

        # Vérifier si un jour a été sélectionné
        if 'schedule_day' not in context.user_data:
            await update.message.reply_text(
                "❌ Veuillez d'abord sélectionner un jour (Aujourd'hui ou Demain).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="schedule_send")
                ]])
            )
            return SCHEDULE_SEND

        # Vérifier si nous avons des posts à planifier
        posts = context.user_data.get("posts", [])
        if not posts and 'current_scheduled_post' not in context.user_data:
            await update.message.reply_text(
                "❌ Aucun contenu à planifier. Veuillez d'abord envoyer du contenu.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU

        # Parser l'heure
        time_text = update.message.text.strip()
        try:
            if ':' in time_text:
                hour, minute = map(int, time_text.split(':'))
            else:
                    hour = int(time_text)
                    minute = 0
        except ValueError:
            await update.message.reply_text(
                "❌ Format d'heure invalide. Utilisez HH:MM (ex: 14:30) ou HH (ex: 14).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="schedule_send")
                ]])
            )
            return SCHEDULE_SEND

            # Validation de l'heure
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            await update.message.reply_text(
                "❌ Heure invalide. Utilisez un format 24h (00:00 à 23:59).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="schedule_send")
                ]])
            )
            return SCHEDULE_SEND

        # Calcul de l'heure cible (heure locale de l'utilisateur)
        user_id = update.effective_user.id
        from database.manager import DatabaseManager
        db_manager = DatabaseManager()
        
        # Récupérer le fuseau horaire de l'utilisateur
        user_timezone = db_manager.get_user_timezone(user_id)
        if not user_timezone:
            user_timezone = 'Europe/Paris'  # Fallback
            
        import pytz
        tz = pytz.timezone(user_timezone)
        local_now = datetime.now(tz)
        
        target_date_local = local_now.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0
            )

        # Si c'est pour demain, ajouter un jour
        if context.user_data['schedule_day'] == 'tomorrow':
            target_date_local += timedelta(days=1)

        # Vérifier que l'heure n'est pas dans le passé
        if target_date_local <= local_now:
            await update.message.reply_text(
                "❌ L'heure sélectionnée est déjà passée. Choisissez une heure future.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="schedule_send")
                ]])
            )
            return SCHEDULE_SEND

        success_count = 0
        channel_id = context.user_data.get("selected_channel", {}).get("id")

        # Si nous modifions un post existant
        if 'current_scheduled_post' in context.user_data:
            # Logique de modification (existante)
            pass
        else:
            # Planifier chaque nouveau post
            scheduler_manager = get_scheduler_manager()
            
            for post in posts:
                try:
                    # Sauvegarder le post en base de données
                    with sqlite3.connect(settings.db_config["path"]) as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            """
                            INSERT INTO posts (channel_id, post_type, content, caption, scheduled_time)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (channel_id, post['type'], post['content'],
                             post.get('caption'), target_date_local.strftime('%Y-%m-%d %H:%M:%S'))
                        )
                        post_id = cursor.lastrowid
                        conn.commit()

                    # Créer le job de planification
                    job_id = f"post_{post_id}"
                    
                    # ✅ CORRECTION : Créer une fonction wrapper synchrone simple
                    def send_post_job(post_id=post_id):
                        """Fonction wrapper pour envoyer un post planifié"""
                        import asyncio
                        try:
                            # Créer une nouvelle boucle pour le job
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            
                            # Fonction asynchrone pour envoyer le post
                            async def send_post_async():
                                from utils.scheduler_utils import send_scheduled_file
                                post_dict = {"id": post_id}
                                await send_scheduled_file(post_dict, context.application)
                            
                            # Exécuter la fonction asynchrone
                            loop.run_until_complete(send_post_async())
                            loop.close()
                            
                            logger.info(f"✅ Post {post_id} envoyé avec succès")
                            
                        except Exception as job_error:
                            logger.error(f"❌ Erreur dans le job {post_id}: {job_error}")
                            logger.exception("Traceback:")

                    # Planifier le job
                    if scheduler_manager:
                        scheduler_manager.scheduler.add_job(
                            func=send_post_job,
                            trigger="date",
                            run_date=target_date_local,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(f"✅ Job {job_id} créé pour {target_date_local}")
                        success_count += 1
                    else:
                        logger.error("❌ Scheduler manager introuvable")

                except Exception as e:
                    logger.error(f"❌ Erreur lors de la planification du post: {e}")

        # Message de confirmation
        if success_count > 0:
            time_display = target_date_local.strftime("%H:%M")
            day_text = "aujourd'hui" if context.user_data['schedule_day'] == 'today' else "demain"
            
            # Afficher le fuseau horaire utilisé
            timezone_display = user_timezone.split('/')[-1] if '/' in user_timezone else user_timezone
            
            await update.message.reply_text(
                f"✅ {success_count} fichier(s) programmé(s) pour {day_text} à {time_display}\n"
                f"🌍 Fuseau horaire : {timezone_display} ({user_timezone})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
        else:
            await update.message.reply_text(
                "❌ Erreur lors de la planification.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )

        # Nettoyage du contexte
        context.user_data.clear()
        return MAIN_MENU

    except Exception as e:
        logger.error(f"❌ Erreur dans handle_schedule_time: {e}")
        logger.exception("Traceback complet:")
        try:
            await update.message.reply_text(
                "❌ Une erreur est survenue lors de la planification.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
        except:
            pass
        return MAIN_MENU
async def handle_edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la modification de l'heure d'une publication planifiée"""
    try:
        query = update.callback_query
        await query.answer()

        post = context.user_data.get('current_scheduled_post')
        if not post:
            await query.edit_message_text(
                "❌ Publication introuvable.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU

        keyboard = [
            [
                InlineKeyboardButton("Aujourd'hui", callback_data="schedule_today"),
                InlineKeyboardButton("Demain", callback_data="schedule_tomorrow"),
            ],
            [InlineKeyboardButton("↩️ Retour", callback_data="retour")]
        ]

        message_text = (
            "📅 Choisissez la nouvelle date pour votre publication :\n\n"
            "1️⃣ Sélectionnez le jour (Aujourd'hui ou Demain)\n"
            "2️⃣ Envoyez-moi l'heure au format :\n"
            "   • '15:30' ou '1530' (24h)\n"
            "   • '6' (06:00)\n"
            "   • '5 3' (05:03)\n\n"
            "❌ Aucun jour sélectionné"
        )

        await query.edit_message_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        context.user_data['editing_post_id'] = post['id']
        return SCHEDULE_SEND

    except Exception as e:
        logger.error(f"Erreur dans handle_edit_time : {e}")
        await query.edit_message_text(
            "❌ Une erreur est survenue lors de la modification de l'heure.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU


async def handle_cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annule une publication planifiée"""
    try:
        query = update.callback_query
        await query.answer()

        post = context.user_data.get('current_scheduled_post')
        if not post:
            await query.edit_message_text(
                "❌ Publication introuvable.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU

        keyboard = [
            [
                InlineKeyboardButton("✅ Oui, annuler", callback_data="confirm_cancel"),
                InlineKeyboardButton("❌ Non, garder", callback_data="retour")
            ]
        ]

        await query.edit_message_text(
            "⚠️ Êtes-vous sûr de vouloir annuler cette publication ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SCHEDULE_SELECT_CHANNEL

    except Exception as e:
        logger.error(f"Erreur dans handle_cancel_post : {e}")
        await query.edit_message_text(
            "❌ Une erreur est survenue lors de l'annulation.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU


async def handle_confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirme et supprime une publication planifiée"""
    query = update.callback_query
    await query.answer()
    
    try:
        if 'current_scheduled_post' not in context.user_data:
            await query.message.reply_text("❌ Aucun post planifié sélectionné.")
            return MAIN_MENU
            
        post = context.user_data['current_scheduled_post']
        
        # Supprimer de la base de données
        with sqlite3.connect(settings.db_config["path"]) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM posts WHERE id = ?", (post['id'],))
            conn.commit()
            
        # Supprimer le job du scheduler
        job_id = f"post_{post['id']}"
        # Utiliser l'application depuis le contexte
        if context.application and hasattr(context.application, 'job_queue'):
            try:
                context.application.job_queue.remove_job(job_id)
            except Exception:
                pass  # Le job n'existe peut-être pas
                
        await query.message.reply_text(
            "✅ Publication planifiée supprimée avec succès!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        
        # Nettoyer les données
        context.user_data.pop('current_scheduled_post', None)
        return MAIN_MENU
        
    except Exception as e:
        logger.error(f"Erreur lors de la suppression: {e}")
        await query.message.reply_text(
            "❌ Erreur lors de la suppression de la publication.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU



async def manage_channels_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Affiche le menu de gestion des canaux"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    channels = db_manager.list_channels(user_id)
    
    keyboard = []
    
    if channels:
        for channel in channels:
            keyboard.append([
                InlineKeyboardButton(
                    f"📺 {channel['name']} (@{channel['username']})",
                    callback_data=f"channel_{channel['username']}"
                )
            ])
    
    keyboard.extend([
        [InlineKeyboardButton("➕ Ajouter un canal", callback_data="add_channel")],
        [InlineKeyboardButton("↩️ Retour", callback_data="settings")]
    ])
    
    message_text = "🌐 **Gestion des canaux**\n\n"
    if channels:
        message_text += "Sélectionnez un canal pour le gérer ou ajoutez-en un nouveau."
    else:
        message_text += "Vous n'avez aucun canal configuré. Ajoutez-en un pour commencer."
    
    await query.edit_message_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    return SETTINGS


async def add_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Demande à l'utilisateur d'entrer les informations du canal"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "➕ **Ajouter un canal**\n\n"
        "Envoyez-moi le nom du canal suivi de son @username.\n"
        "Format : `Nom du canal @username`\n\n"
        "Exemple : `Mon Canal @monchannel`\n\n"
        "⚠️ Assurez-vous d'être administrateur du canal.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Annuler", callback_data="manage_channels")
        ]])
    )
    
    context.user_data['waiting_for_channel_info'] = True
    return WAITING_CHANNEL_INFO


async def use_default_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Utilise le canal par défaut du bot"""
    query = update.callback_query
    await query.answer()
    
    # Créer un canal temporaire par défaut
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    try:
        # Vérifier si un canal par défaut existe déjà
        default_channel = db_manager.get_channel_by_username("@default_channel", user_id)
        
        if not default_channel:
            # Créer le canal par défaut
            channel_id = db_manager.add_channel("Canal par défaut", "@default_channel", user_id)
            
        await query.edit_message_text(
            "✅ Canal par défaut activé!\n\n"
            "Vous pouvez maintenant créer des publications.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📝 Créer une publication", callback_data="create_publication"),
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        
    except Exception as e:
        logger.error(f"Erreur lors de la création du canal par défaut: {e}")
        await query.edit_message_text(
            "❌ Erreur lors de la création du canal par défaut.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
    
    return MAIN_MENU


async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_username: str) -> int:
    """Sélectionne un canal pour créer une publication"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    # Récupérer les infos du canal
    channel = db_manager.get_channel_by_username(channel_username, user_id)
    
    if not channel:
        await query.edit_message_text(
            "❌ Canal non trouvé.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU
    
    # Stocker le canal sélectionné
    context.user_data['selected_channel'] = channel
    
    await query.edit_message_text(
        f"📺 Canal sélectionné : **{channel['name']}**\n\n"
        f"Envoyez-moi maintenant votre contenu :\n"
        f"• 📝 Texte\n"
        f"• 🖼️ Photo\n"
        f"• 🎥 Vidéo\n"
        f"• 📄 Document",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Annuler", callback_data="create_publication")
        ]])
    )
    
    return WAITING_PUBLICATION_CONTENT


async def show_channel_options(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_username: str) -> int:
    """Affiche les options pour un canal spécifique"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    channel = db_manager.get_channel_by_username(channel_username, user_id)
    
    if not channel:
        await query.edit_message_text(
            "❌ Canal non trouvé.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Retour", callback_data="manage_channels")
            ]])
        )
        return SETTINGS
    
    keyboard = [
        [InlineKeyboardButton("⚙️ Paramètres personnalisés", callback_data=f"custom_channel_{channel_username}")],
        [InlineKeyboardButton("📝 Créer une publication", callback_data=f"select_channel_{channel_username}")],
        [InlineKeyboardButton("🗑️ Supprimer le canal", callback_data=f"delete_channel_{channel['id']}")],
        [InlineKeyboardButton("↩️ Retour", callback_data="manage_channels")]
    ]
    
    await query.edit_message_text(
        f"📺 **{channel['name']}** (@{channel['username']})\n\n"
        f"Que voulez-vous faire avec ce canal ?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    return SETTINGS


async def custom_channel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_username: str) -> int:
    """Affiche les paramètres personnalisés d'un canal"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    channel = db_manager.get_channel_by_username(channel_username, user_id)
    
    if not channel:
        await query.edit_message_text(
            "❌ Canal non trouvé.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Retour", callback_data="manage_channels")
            ]])
        )
        return SETTINGS
    
    # Récupérer les infos du thumbnail et du tag
    db_manager = DatabaseManager()
    thumbnail_exists = db_manager.get_thumbnail(channel_username, user_id) is not None
    tag = db_manager.get_channel_tag(channel_username, user_id)
    
    keyboard = [
        [InlineKeyboardButton("🖼️ Gérer le thumbnail", callback_data="thumbnail_menu")],
        [InlineKeyboardButton("🏷️ Ajouter un hashtag", callback_data=f"edit_tag_{channel_username}")],
        [InlineKeyboardButton("↩️ Retour", callback_data=f"channel_{channel_username}")]
    ]
    
    message_text = f"⚙️ **Paramètres de {channel['name']}**\n\n"
    message_text += f"🖼️ Thumbnail : {'✅ Défini' if thumbnail_exists else '❌ Non défini'}\n"
    message_text += f"🏷️ Tag : {tag if tag else 'Aucun tag défini'}\n"
    
    # Stocker le canal dans le contexte pour les opérations suivantes
    context.user_data['custom_channel'] = channel_username
    
    await query.edit_message_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    return SETTINGS


async def handle_edit_tag(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_username: str) -> int:
    """Gère l'ajout/modification de hashtags pour un canal"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    # Stocker le canal dans le contexte pour la prochaine étape
    context.user_data['editing_tag_for_channel'] = channel_username
    context.user_data['awaiting_username'] = True
    
    message_text = (
        "Send me the text/tag to add to your files.\n\n"
        "You can send:\n"
        "• @username\n"
        "• #hashtag\n"
        "• [📢 @channel]\n"
        "• 🔥 @fire\n"
        "• Any text with emojis!"
    )
    
    keyboard = [
        [InlineKeyboardButton("❌ Annuler", callback_data=f"custom_channel_{channel_username}")]
    ]
    
    await query.edit_message_text(
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return WAITING_TAG_INPUT


async def custom_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menu des paramètres personnalisés généraux"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🌐 Gérer les canaux", callback_data="manage_channels")],
        [InlineKeyboardButton("🕐 Fuseau horaire", callback_data="timezone_settings")],
        [InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        "⚙️ **Paramètres personnalisés**\n\n"
        "Choisissez une option :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    return SETTINGS


async def delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> int:
    """Demande confirmation pour supprimer un canal"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"confirm_delete_channel_{channel_id}"),
            InlineKeyboardButton("❌ Non, annuler", callback_data="manage_channels")
        ]
    ]
    
    await query.edit_message_text(
        "⚠️ **Êtes-vous sûr de vouloir supprimer ce canal ?**\n\n"
        "Cette action supprimera également toutes les publications associées.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    return SETTINGS


async def confirm_delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> int:
    """Confirme et supprime le canal"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    try:
        channel_id_int = int(channel_id)
        success = db_manager.delete_channel(channel_id_int, user_id)
        
        if success:
            await query.edit_message_text(
                "✅ Canal supprimé avec succès!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="manage_channels")
                ]])
            )
        else:
            await query.edit_message_text(
                "❌ Erreur lors de la suppression du canal.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="manage_channels")
                ]])
            )
    except Exception as e:
        logger.error(f"Erreur lors de la suppression du canal: {e}")
        await query.edit_message_text(
            "❌ Une erreur est survenue.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Retour", callback_data="manage_channels")
            ]])
        )
    
    return SETTINGS


async def handle_timezone_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les paramètres de fuseau horaire"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    # Récupérer le fuseau horaire actuel
    current_timezone = db_manager.get_user_timezone(user_id) or "UTC"
    
    # Liste des fuseaux horaires courants
    popular_timezones = [
        ("Europe/Paris", "🇫🇷 Paris (UTC+1/+2)"),
        ("Europe/London", "🇬🇧 Londres (UTC+0/+1)"),
        ("America/New_York", "🇺🇸 New York (UTC-5/-4)"),
        ("America/Los_Angeles", "🇺🇸 Los Angeles (UTC-8/-7)"),
        ("Asia/Tokyo", "🇯🇵 Tokyo (UTC+9)"),
        ("Asia/Shanghai", "🇨🇳 Shanghai (UTC+8)"),
        ("Australia/Sydney", "🇦🇺 Sydney (UTC+10/+11)"),
        ("UTC", "🌐 UTC (Temps universel)")
    ]
    
    keyboard = []
    for tz_code, tz_name in popular_timezones:
        # Marquer le fuseau actuel
        if tz_code == current_timezone:
            button_text = f"✅ {tz_name}"
        else:
            button_text = tz_name
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_timezone_{tz_code}")])
    
    # Bouton pour saisir manuellement
    keyboard.append([InlineKeyboardButton("✏️ Saisir manuellement", callback_data="manual_timezone")])
    keyboard.append([InlineKeyboardButton("↩️ Retour", callback_data="custom_settings")])
    
    # Obtenir l'heure actuelle dans le fuseau de l'utilisateur
    from datetime import datetime
    import pytz
    
    try:
        user_tz = pytz.timezone(current_timezone)
        local_time = datetime.now(user_tz)
        time_display = local_time.strftime("%H:%M")
        date_display = local_time.strftime("%d/%m/%Y")
    except:
        time_display = "Erreur"
        date_display = ""
    
    message = (
        f"🕐 Configuration du fuseau horaire\n\n"
        f"Fuseau actuel : {current_timezone}\n"
        f"Heure locale : {time_display} ({date_display})\n\n"
        f"Sélectionnez votre fuseau horaire pour que les messages soient planifiés selon votre heure locale :"
    )
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return SETTINGS


async def handle_set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE, timezone_code: str) -> int:
    """Définit le fuseau horaire sélectionné"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db_manager = DatabaseManager()
    
    try:
        # Valider que le fuseau horaire existe
        import pytz
        if timezone_code not in pytz.all_timezones:
            await query.edit_message_text(
                "❌ Fuseau horaire invalide.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="timezone_settings")
                ]])
            )
            return SETTINGS
        
        # Sauvegarder le fuseau horaire
        success = db_manager.set_user_timezone(user_id, timezone_code)
        
        if success:
            # Afficher l'heure dans le nouveau fuseau
            from datetime import datetime
            user_tz = pytz.timezone(timezone_code)
            local_time = datetime.now(user_tz)
            
            # Message sans Markdown pour éviter les erreurs de parsing
            await query.edit_message_text(
                f"✅ Fuseau horaire mis à jour !\n\n"
                f"Nouveau fuseau : {timezone_code}\n"
                f"Heure locale : {local_time.strftime('%H:%M')} ({local_time.strftime('%d/%m/%Y')})\n\n"
                f"Vos futures publications seront planifiées selon ce fuseau horaire.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour aux paramètres", callback_data="settings")
                ]])
            )
        else:
            await query.edit_message_text(
                "❌ Erreur lors de la mise à jour du fuseau horaire.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Retour", callback_data="timezone_settings")
                ]])
            )
            
    except Exception as e:
        logger.error(f"Erreur lors de la mise à jour du fuseau horaire : {e}")
        await query.edit_message_text(
            "❌ Une erreur est survenue.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Retour", callback_data="timezone_settings")
            ]])
        )
    
    return SETTINGS


async def handle_manual_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Permet la saisie manuelle du fuseau horaire"""
    query = update.callback_query
    await query.answer()
    
    # Stocker qu'on attend une saisie de fuseau horaire
    context.user_data['waiting_for_timezone'] = True
    
    keyboard = [[InlineKeyboardButton("❌ Annuler", callback_data="timezone_settings")]]
    
    await query.edit_message_text(
        "✏️ Saisie manuelle du fuseau horaire\n\n"
        "Envoyez-moi votre fuseau horaire au format standard.\n\n"
        "Exemples :\n"
        "• Europe/Paris\n"
        "• America/New_York\n"
        "• Asia/Tokyo\n"
        "• Africa/Cairo\n\n"
        "💡 Vous pouvez trouver la liste complète sur:\n"
        "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Importer l'état pour attendre la saisie
    from conversation_states import WAITING_TAG_INPUT
    return WAITING_TAG_INPUT  # On réutilise cet état pour la saisie de texte


async def handle_create_publication(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la création d'une nouvelle publication"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    try:
        logger.info(f"handle_create_publication appelé par l'utilisateur {user_id}")
        
        # Récupération des canaux depuis la base de données
        db_manager = DatabaseManager()
        channels = db_manager.list_channels(user_id)
        logger.info(f"Canaux trouvés pour l'utilisateur {user_id}: {channels}")
        
        # Si aucun canal n'est configuré
        if not channels:
            keyboard = [
                [InlineKeyboardButton("➕ Ajouter un canal", callback_data="add_channel")],
                [InlineKeyboardButton("🔄 Utiliser le canal par défaut", callback_data="use_default_channel")],
                [InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]
            ]
            
            message_text = (
                "⚠️ Aucun canal configuré\n\n"
                "Pour publier du contenu, vous devez d'abord configurer un canal Telegram.\n"
                "Vous pouvez soit :\n"
                "• Ajouter un canal existant dont vous êtes administrateur\n"
                "• Utiliser le canal par défaut (temporaire)"
            )
            
            await query.edit_message_text(
                message_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
            return WAITING_CHANNEL_SELECTION
        
        # Construction du clavier avec les canaux
        keyboard = []
        current_row = []
        
        for i, channel in enumerate(channels):
            current_row.append(InlineKeyboardButton(
                channel['name'],
                callback_data=f"select_channel_{channel['username']}"
            ))
            
            # Nouvelle ligne tous les 2 boutons
            if len(current_row) == 2 or i == len(channels) - 1:
                keyboard.append(current_row)
                current_row = []
        
        # Ajout des boutons d'action
        keyboard.append([
            InlineKeyboardButton("➕ Ajouter un canal", callback_data="add_channel")
        ])
        keyboard.append([
            InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
        ])
        
        message_text = (
            "📝 Sélectionnez un canal pour votre publication :\n\n"
            "• Choisissez un canal existant, ou\n"
            "• Ajoutez un nouveau canal"
        )
        
        await query.edit_message_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return WAITING_CHANNEL_SELECTION
        
    except Exception as e:
        logger.error(f"Erreur lors de l'affichage des canaux: {e}")
        
        keyboard = [[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]
        error_message = "❌ Une erreur est survenue lors de la récupération des canaux."
        
        await query.edit_message_text(
            error_message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return MAIN_MENU


async def planifier_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les publications planifiées par chaîne."""
    try:
        # Initialiser le gestionnaire de base de données
        db_manager = DatabaseManager()
        
        with sqlite3.connect(settings.db_config["path"]) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.id, p.type, p.content, p.caption, p.scheduled_time, c.name, c.username
                FROM posts p
                JOIN channels c ON p.channel_id = c.id
                WHERE p.scheduled_time > datetime('now')
                ORDER BY p.scheduled_time
            """)
            scheduled_posts = cursor.fetchall()

        if not scheduled_posts:
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "❌ Aucun post planifié trouvé.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                    ]])
                )
            else:
                await update.message.reply_text("❌ Aucun post planifié trouvé.")
            return MAIN_MENU

        keyboard = []
        user_id = update.effective_user.id
        user_timezone = db_manager.get_user_timezone(user_id) or "UTC"
        local_tz = pytz.timezone(user_timezone)

        message = "📅 Publications planifiées :\n\n"

        for post in scheduled_posts:
            post_id, post_type, content, caption, scheduled_time, channel_name, channel_username = post
            scheduled_datetime = datetime.strptime(scheduled_time, '%Y-%m-%d %H:%M:%S')
            local_time = scheduled_datetime.replace(tzinfo=pytz.UTC).astimezone(local_tz)

            button_text = f"{local_time.strftime('%d/%m/%Y %H:%M')} - {channel_name} (@{channel_username})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"show_post_{post_id}")])
            message += f"• {button_text}\n"

        keyboard.append([InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")])

        if update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        return SCHEDULE_SELECT_CHANNEL

    except Exception as e:
        logger.error(f"Erreur dans planifier_post : {e}")
        error_message = "❌ Une erreur est survenue lors de l'affichage des publications planifiées."
        if update.callback_query:
            await update.callback_query.edit_message_text(
                error_message,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
        else:
            await update.message.reply_text(error_message)
        return MAIN_MENU


async def show_scheduled_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les détails d'une publication planifiée"""
    try:
        query = update.callback_query
        await query.answer()

        post_id = query.data.split('_')[-1]

        with sqlite3.connect(settings.db_config["path"]) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.id, p.type, p.content, p.caption, p.scheduled_time, c.name, c.username
                FROM posts p
                JOIN channels c ON p.channel_id = c.id
                WHERE p.id = ?
            """, (post_id,))
            post_data = cursor.fetchone()

        if not post_data:
            await query.edit_message_text(
                "❌ Publication introuvable.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU

        scheduled_time = datetime.strptime(post_data[4], '%Y-%m-%d %H:%M:%S')

        post = {
            'id': post_data[0],
            'type': post_data[1],
            'content': post_data[2],
            'caption': post_data[3],
            'scheduled_time': post_data[4],
            'channel_name': post_data[5],
            'channel_username': post_data[6],
            'scheduled_date': scheduled_time
        }

        context.user_data['current_scheduled_post'] = post

        keyboard = [
            [InlineKeyboardButton("🕒 Modifier l'heure", callback_data="modifier_heure")],
            [InlineKeyboardButton("🚀 Envoyer maintenant", callback_data="envoyer_maintenant")],
            [InlineKeyboardButton("❌ Annuler la publication", callback_data="annuler_publication")],
            [InlineKeyboardButton("↩️ Retour", callback_data="retour")]
        ]

        try:
            if post['type'] == "photo":
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=post['content'],
                    caption=post.get('caption'),
                    reply_markup=None
                )
            elif post['type'] == "video":
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=post['content'],
                    caption=post.get('caption'),
                    reply_markup=None
                )
            elif post['type'] == "document":
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=post['content'],
                    caption=post.get('caption'),
                    reply_markup=None
                )
            elif post['type'] == "text":
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=post['content'],
                    reply_markup=None
                )
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi du contenu : {e}")

        # Récupérer le fuseau horaire de l'utilisateur
        user_id = update.effective_user.id
        db_manager = DatabaseManager()
        user_timezone = db_manager.get_user_timezone(user_id) or "UTC"
        local_tz = pytz.timezone(user_timezone)
        
        # Convertir l'heure en local
        utc_time = scheduled_time.replace(tzinfo=pytz.UTC)
        local_time = utc_time.astimezone(local_tz)

        message = (
            f"📝 Publication planifiée :\n\n"
            f"📅 Date : {local_time.strftime('%d/%m/%Y')}\n"
            f"⏰ Heure : {local_time.strftime('%H:%M')} ({user_timezone})\n"
            f"🌐 Heure UTC : {scheduled_time.strftime('%H:%M')}\n"
            f"📍 Canal : {post['channel_name']}\n"
            f"📎 Type : {post['type']}\n"
        )

        await query.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return SCHEDULE_SELECT_CHANNEL

    except Exception as e:
        logger.error(f"Erreur dans show_scheduled_post : {e}")
        await query.edit_message_text(
            "❌ Une erreur est survenue lors de l'affichage de la publication.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU


async def show_edit_file_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, post_index: int) -> int:
    """Affiche le menu d'édition de fichier avec les 3 options"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    try:
        # Vérifier que le post existe
        if 'posts' not in context.user_data or post_index >= len(context.user_data['posts']):
            from utils.message_utils import safe_edit_message_text
            await safe_edit_message_text(
                query,
                "❌ Post introuvable.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU
        
        post = context.user_data['posts'][post_index]
        
        # Menu d'édition exactement comme dans l'image
        keyboard = [
            [InlineKeyboardButton("✏️ Rename", callback_data=f"rename_post_{post_index}")],
            [InlineKeyboardButton("🖼️ Add Thumbnail", callback_data=f"add_thumbnail_{post_index}")],
            [InlineKeyboardButton("🖼️ Add Thumbnail + Rename", callback_data=f"add_thumb_{post_index}")]
        ]
        
        from utils.message_utils import safe_edit_message_text
        await safe_edit_message_text(
            query,
            f"✏️ **Édition du fichier {post_index + 1}**\n\n"
            f"Type: {post['type']}\n"
            f"Choisissez une action :",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        
        return MAIN_MENU
        
    except Exception as e:
        logger.error(f"Erreur dans show_edit_file_menu: {e}")
        from utils.message_utils import safe_edit_message_text
        await safe_edit_message_text(
            query,
            "❌ Une erreur est survenue.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU


async def handle_rename_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_index: int) -> int:
    """Gère le renommage d'un post"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Vérifier que le post existe
        if 'posts' not in context.user_data or post_index >= len(context.user_data['posts']):
            await safe_edit_callback_message(
                query,
                "❌ Post introuvable.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return MAIN_MENU
        
        # Stocker les variables nécessaires pour le gestionnaire existant
        context.user_data['waiting_for_rename'] = True
        context.user_data['current_post_index'] = post_index
        
        # Stocker l'ID du message actuel pour le supprimer plus tard
        context.user_data['rename_prompt_message_id'] = query.message.message_id
        
        from utils.message_utils import safe_edit_message_text
        await safe_edit_message_text(
            query,
            f"✏️ **Renommer le fichier {post_index + 1}**\n\n"
            "Envoyez le nouveau nom/titre pour ce fichier :",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Annuler", callback_data="main_menu")
            ]]),
            parse_mode="Markdown"
        )
        
        # Importer ici pour éviter l'import circulaire
        from conversation_states import WAITING_RENAME_INPUT
        return WAITING_RENAME_INPUT
        
    except Exception as e:
        logger.error(f"Erreur dans handle_rename_post: {e}")
        from utils.message_utils import safe_edit_message_text
        await safe_edit_message_text(
            query,
            "❌ Une erreur est survenue."
        )
        return MAIN_MENU


async def handle_add_thumbnail_to_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, post_index: int) -> int:
    """Ajoute un thumbnail à un post via callback"""
    query = update.callback_query
    
    try:
        # Stocker l'ID du message actuel pour le supprimer plus tard
        context.user_data['thumbnail_prompt_message_id'] = query.message.message_id
        
        # Utiliser la nouvelle fonction centralisée pour tout le traitement
        logger.info(f"🎯 handle_add_thumbnail_to_post_callback appelé pour post {post_index + 1}")
        
        # Appeler la fonction centralisée qui fait tout le travail
        success = await process_thumbnail_and_upload(update, context, post_index)
        
        if success:
            logger.info(f"✅ Traitement thumbnail réussi pour post {post_index + 1}")
            return WAITING_PUBLICATION_CONTENT
        else:
            logger.error(f"❌ Échec du traitement thumbnail pour post {post_index + 1}")
            return MAIN_MENU
            
    except Exception as e:
        logger.error(f"Erreur dans handle_add_thumbnail_to_post_callback: {e}")
        await safe_edit_callback_message(
            query,
            "❌ Une erreur est survenue lors de l'ajout du thumbnail.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]])
        )
        return MAIN_MENU





async def handle_delete_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_index: int) -> int:
    """Supprime un post spécifique"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Vérifier que le post existe
        if 'posts' not in context.user_data or post_index >= len(context.user_data['posts']):
            # Envoyer un nouveau message au lieu d'éditer (évite l'erreur "no text to edit")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Post introuvable."
            )
            return WAITING_PUBLICATION_CONTENT
        
        # Récupérer les informations du post avant suppression
        deleted_post = context.user_data['posts'][post_index]
        post_type = deleted_post.get('type', 'contenu')
        
        # Supprimer le post
        context.user_data['posts'].pop(post_index)
        
        # Message de confirmation simple
        remaining_posts = len(context.user_data.get('posts', []))
        message = f"✅ Post {post_index + 1} supprimé\n\n"
        message += f"📝 Type: {post_type}\n\n"
        message += f"Il vous reste {remaining_posts} post(s) en attente"
        
        # Envoyer un nouveau message au lieu d'éditer pour éviter les erreurs
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message
        )
        
        return WAITING_PUBLICATION_CONTENT
        
    except Exception as e:
        logger.error(f"Erreur dans handle_delete_post: {e}")
        # Envoyer un nouveau message en cas d'erreur
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Erreur lors de la suppression du post."
        )
        return WAITING_PUBLICATION_CONTENT

async def schedule_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interface de planification des messages"""
    try:
        query = update.callback_query
        await query.answer()

        # NETTOYAGE : Supprimer les anciennes données de planification pour éviter les conflits
        context.user_data.pop('schedule_day', None)
        logger.info("🧹 Contexte de planification nettoyé")

        # Récupérer le jour sélectionné s'il existe (après nettoyage, ce sera None)
        selected_day = context.user_data.get('schedule_day', None)

        # Créer les boutons avec les emojis appropriés
        keyboard = [
            [
                InlineKeyboardButton(
                    f"Aujourd'hui {'✅' if selected_day == 'today' else ''}",
                    callback_data="schedule_today"
                ),
                InlineKeyboardButton(
                    f"Demain {'✅' if selected_day == 'tomorrow' else ''}",
                    callback_data="schedule_tomorrow"
                ),
            ],
            [InlineKeyboardButton("↩️ Retour", callback_data="send_post")],
        ]

        # Construction du message
        day_status = "✅ Jour sélectionné : " + (
            "Aujourd'hui" if selected_day == "today" else "Demain") if selected_day else "❌ Aucun jour sélectionné"

        message_text = (
            "📅 Choisissez quand envoyer votre publication :\n\n"
            "1️⃣ Sélectionnez le jour (Aujourd'hui ou Demain)\n"
            "2️⃣ Envoyez-moi l'heure au format :\n"
            "   • '15:30' ou '1530' (24h)\n"
            "   • '6' (06:00)\n"
            "   • '5 3' (05:03)\n\n"
            f"{day_status}"
        )

        await safe_edit_callback_message(
            query,
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SCHEDULE_SEND
    except Exception as e:
        logger.error(f"Erreur lors de la planification de l'envoi : {e}")
        await safe_edit_callback_message(
            query,
            "❌ Erreur lors de la planification.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Retour", callback_data="main_menu")]])
        )
        return MAIN_MENU

async def send_post_now(update, context, scheduled_post=None):
    """Envoie le post immédiatement en utilisant le meilleur client disponible."""
    try:
        logger.info("🔥 DEBUG: send_post_now appelée - DEBUT")
        logger.info("🚀 === DÉBUT send_post_now ===")
        
        # 🔍 DIAGNOSTIC - Vérifier l'état des clients
        try:
            from utils.clients import client_manager
            logger.info("🔍 DIAGNOSTIC: Vérification des clients...")
            
            # Vérifier Pyrogram
            try:
                pyro_client = await client_manager.get_pyrogram_client()
                pyro_status = "✅ Connecté" if pyro_client and hasattr(pyro_client, 'me') else "❌ Non connecté"
                logger.info(f"📱 Pyrogram: {pyro_status}")
            except Exception as e:
                logger.warning(f"📱 Pyrogram: ❌ Erreur - {e}")
            
            # Vérifier Telethon
            try:
                telethon_client = await client_manager.get_telethon_client()
                telethon_status = "✅ Connecté" if telethon_client and telethon_client.is_connected() else "❌ Non connecté"
                logger.info(f"📱 Telethon: {telethon_status}")
            except Exception as e:
                logger.warning(f"📱 Telethon: ❌ Erreur - {e}")
                
            # Vérifier API Bot
            try:
                bot_info = await context.bot.get_me()
                logger.info(f"📱 API Bot: ✅ Connecté (@{bot_info.username})")
            except Exception as e:
                logger.warning(f"📱 API Bot: ❌ Erreur - {e}")
                
        except Exception as diagnostic_error:
            logger.warning(f"🔍 Erreur diagnostic clients: {diagnostic_error}")
        
        if scheduled_post:
            posts = [scheduled_post]
            channel = scheduled_post.get('channel', '@default_channel')  # Correction: canal par défaut fixe
        else:
            posts = context.user_data.get("posts", [])
            logger.info(f"📊 Posts trouvés: {len(posts)}")
            
            if not posts:
                logger.info("❌ Aucun post à envoyer")
                if update.message:
                    await update.message.reply_text(
                        "❌ Il n'y a pas de fichiers à envoyer.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                        ]])
                    )
                elif hasattr(update, 'callback_query') and update.callback_query:
                    await update.callback_query.message.reply_text(
                        "❌ Il n'y a pas de fichiers à envoyer.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                        ]])
                    )
                return MAIN_MENU
            
            # Récupérer le canal du premier post ou du canal sélectionné
            selected_channel = context.user_data.get('selected_channel', {})
            channel = posts[0].get("channel") or selected_channel.get('username', '@default_channel')

        logger.info(f"📺 Canal cible: {channel}")

        # Correction : ajouter @ si besoin pour les canaux publics
        if isinstance(channel, str) and not channel.startswith('@') and not channel.startswith('-100'):
            channel = '@' + channel

        logger.info(f"📺 Canal final: {channel}")

        success_count = 0  # Initialisation du compteur de succès
        for post_index, post in enumerate(posts):
            logger.info(f"📤 Envoi post {post_index + 1}/{len(posts)}")
            
            post_type = post.get("type")
            content = post.get("content")
            caption = post.get("caption") or ""
            filename = post.get("filename")
            thumbnail = post.get('thumbnail')

            logger.info(f"📝 Type: {post_type}")
            logger.info(f"🖼️ DEBUG THUMBNAIL: {thumbnail}")  # NOUVEAU LOG

            # Ajout du texte custom si défini pour ce canal
            custom_usernames = context.user_data.get('custom_usernames', {})
            channel_username = post.get("channel")
            custom_text = custom_usernames.get(channel_username)
            if custom_text:
                if caption:
                    caption = f"{caption}\n{custom_text}"
                else:
                    caption = custom_text

            # Ajout des hashtags du canal s'ils sont définis
            try:
                user_id = update.effective_user.id if hasattr(update, 'effective_user') and update.effective_user else None
                if user_id and channel_username:
                    db_manager = DatabaseManager()
                    # Nettoyer le nom du canal (enlever @ si présent)
                    clean_channel = channel_username.lstrip('@')
                    hashtags = db_manager.get_channel_tag(clean_channel, user_id)
                    if hashtags and hashtags.strip():
                        logger.info(f"🏷️ Hashtags trouvés pour {clean_channel}: {hashtags}")
                        if caption:
                            caption = f"{caption}\n\n{hashtags}"
                        else:
                            caption = hashtags
                    else:
                        logger.info(f"🏷️ Aucun hashtag défini pour {clean_channel}")
            except Exception as hashtag_error:
                logger.error(f"❌ Erreur récupération hashtags: {hashtag_error}")

            # Récupérer le temps d'auto-destruction s'il est configuré
            auto_destruction_time = context.user_data.get('auto_destruction_time')

            # Utiliser le nouveau handler intelligent pour l'envoi
            from .media_handler import send_file_smart
            
            if post_type in ["photo", "video", "document"]:
                logger.info(f"📤 Envoi fichier {post_type}")

                # Cas 1 : Thumbnail personnalisé déjà appliqué
                if post.get('has_custom_thumbnail'):
                    logger.info(f"🎨 Envoi du post {post_index + 1} avec thumbnail personnalisé")
                    
                    # ✅ CONSTRUIRE LE CLAVIER AVEC RÉACTIONS ET BOUTONS
                    keyboard = []
                    
                    # Ajouter les réactions
                    reactions = post.get('reactions', [])
                    if reactions:
                        if isinstance(reactions, str):
                            try:
                                reactions = json.loads(reactions)
                            except json.JSONDecodeError:
                                reactions = []
                        
                        if reactions:
                            current_row = []
                            for reaction in reactions:
                                current_row.append(InlineKeyboardButton(
                                    reaction,
                                    callback_data=f"reaction_{post_index}_{reaction}"
                                ))
                                if len(current_row) == 4:
                                    keyboard.append(current_row)
                                    current_row = []
                            if current_row:
                                keyboard.append(current_row)
                    
                    # Ajouter les boutons URL
                    buttons = post.get('buttons', [])
                    if buttons:
                        if isinstance(buttons, str):
                            try:
                                buttons = json.loads(buttons)
                            except json.JSONDecodeError:
                                buttons = []
                        
                        for button in buttons:
                            if isinstance(button, dict) and 'text' in button and 'url' in button:
                                keyboard.append([InlineKeyboardButton(button['text'], url=button['url'])])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                    logger.info(f"🎯 Clavier créé pour post {post_index + 1}: {len(keyboard)} ligne(s)")
                    
                    # Envoi direct avec le nouveau file_id
                    try:
                        sent_message = None
                        if post_type == "photo":
                            sent_message = await context.bot.send_photo(
                                chat_id=channel,
                                photo=content,
                                caption=caption,
                                reply_markup=reply_markup
                            )
                        elif post_type == "video":
                            sent_message = await context.bot.send_video(
                                chat_id=channel,
                                video=content,
                                caption=caption,
                                reply_markup=reply_markup
                            )
                        elif post_type == "document":
                            sent_message = await context.bot.send_document(
                                chat_id=channel,
                                document=content,
                                caption=caption,
                                reply_markup=reply_markup
                            )
                        if sent_message:
                            logger.info(f"✅ Envoi réussi du post {post_index + 1} avec thumbnail personnalisé")
                            success_count += 1
                            
                            # Programmer l'auto-destruction si configurée
                            if auto_destruction_time and auto_destruction_time > 0:
                                schedule_auto_destruction(context, channel, sent_message.message_id, auto_destruction_time)
                            
                    except Exception as e:
                        logger.error(f"❌ Erreur envoi du post avec thumbnail personnalisé: {e}")
                        continue

                # Cas 2 : Thumbnail défini mais pas encore appliqué
                elif thumbnail and not post.get('has_custom_thumbnail'):
                    logger.warning(f"⚠️ Thumbnail défini mais non appliqué pour le post {post_index + 1}")
                    logger.info("💡 Conseil: Utilisez 'Add Thumbnail' avant d'envoyer pour appliquer le thumbnail")
                    # Afficher un message d'avertissement à l'utilisateur
                    if update.message:
                        await update.message.reply_text(
                            f"⚠️ Vous avez défini un thumbnail pour le post {post_index + 1} mais il n'est pas appliqué. Cliquez sur 'Add Thumbnail' pour l'appliquer.",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]])
                        )
                    elif hasattr(update, 'callback_query') and update.callback_query:
                        await update.callback_query.message.reply_text(
                            f"⚠️ Vous avez défini un thumbnail pour le post {post_index + 1} mais il n'est pas appliqué. Cliquez sur 'Add Thumbnail' pour l'appliquer.",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]])
                        )
                    continue

                # Cas 3 : Envoi simple (pas de thumbnail)
                else:
                    logger.info(f"🚀 Envoi simple du post {post_index + 1} sans thumbnail personnalisé")
                    
                    # ✅ CONSTRUIRE LE CLAVIER AVEC RÉACTIONS ET BOUTONS
                    keyboard = []
                    
                    # Ajouter les réactions
                    reactions = post.get('reactions', [])
                    if reactions:
                        if isinstance(reactions, str):
                            try:
                                reactions = json.loads(reactions)
                            except json.JSONDecodeError:
                                reactions = []
                        
                        if reactions:
                            current_row = []
                            for reaction in reactions:
                                current_row.append(InlineKeyboardButton(
                                    reaction,
                                    callback_data=f"reaction_{post_index}_{reaction}"
                                ))
                                if len(current_row) == 4:
                                    keyboard.append(current_row)
                                    current_row = []
                            if current_row:
                                keyboard.append(current_row)
                    
                    # Ajouter les boutons URL
                    buttons = post.get('buttons', [])
                    if buttons:
                        if isinstance(buttons, str):
                            try:
                                buttons = json.loads(buttons)
                            except json.JSONDecodeError:
                                buttons = []
                        
                        for button in buttons:
                            if isinstance(button, dict) and 'text' in button and 'url' in button:
                                keyboard.append([InlineKeyboardButton(button['text'], url=button['url'])])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                    logger.info(f"🎯 Clavier créé pour post {post_index + 1}: {len(keyboard)} ligne(s)")
                    
                    try:
                        sent_message = None
                        if post_type == "photo":
                            sent_message = await context.bot.send_photo(
                                chat_id=channel,
                                photo=content,
                                caption=caption,
                                reply_markup=reply_markup
                            )
                        elif post_type == "video":
                            sent_message = await context.bot.send_video(
                                chat_id=channel,
                                video=content,
                                caption=caption,
                                reply_markup=reply_markup
                            )
                        elif post_type == "document":
                            sent_message = await context.bot.send_document(
                                chat_id=channel,
                                document=content,
                                caption=caption,
                                reply_markup=reply_markup
                            )
                        if sent_message:
                            logger.info(f"✅ Envoi réussi du post {post_index + 1} sans thumbnail personnalisé")
                            success_count += 1
                            
                            # Programmer l'auto-destruction si configurée
                            if auto_destruction_time and auto_destruction_time > 0:
                                schedule_auto_destruction(context, channel, sent_message.message_id, auto_destruction_time)
                            
                    except Exception as e:
                        logger.error(f"❌ Erreur envoi du post sans thumbnail: {e}")
                        continue
            
            elif post_type == "text":
                logger.info(f"📝 Envoi texte")
                try:
                    sent_message = await context.bot.send_message(
                        chat_id=channel,
                        text=content
                    )
                    if sent_message:
                        logger.info(f"✅ Envoi réussi du post texte {post_index + 1}")
                        success_count += 1
                        
                        # Programmer l'auto-destruction si configurée
                        if auto_destruction_time and auto_destruction_time > 0:
                            schedule_auto_destruction(context, channel, sent_message.message_id, auto_destruction_time)
                                
                except Exception as e:
                    logger.error(f"❌ Erreur envoi du post texte: {e}")
                    continue

            # Nettoyer les données après envoi réussi
            context.user_data['posts'] = []
            context.user_data.pop('selected_channel', None)
            
            # Message de confirmation
            success_message = f"✅ **Envoi réussi !**\n\n{success_count} post(s) envoyé(s) vers {channel}"
            
            # Ajouter info auto-destruction si configurée
            if auto_destruction_time and auto_destruction_time > 0:
                if auto_destruction_time < 3600:
                    time_str = f"{auto_destruction_time // 60} minute(s)"
                elif auto_destruction_time < 86400:
                    time_str = f"{auto_destruction_time // 3600} heure(s)"
                else:
                    time_str = f"{auto_destruction_time // 86400} jour(s)"
                success_message += f"\n\n⏰ **Auto-destruction activée** : {time_str}"
            
            if update.message:
                await update.message.reply_text(
                    success_message,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                    ]]),
                    parse_mode='Markdown'
                )
            elif hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.reply_text(
                    success_message,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                    ]]),
                    parse_mode='Markdown'
                )
            
            logger.info("✅ === FIN send_post_now - SUCCÈS ===")
            return MAIN_MENU
        
    except Exception as e:
        logger.error(f"❌ ERREUR dans send_post_now: {e}")
        logger.exception("Traceback complet:")
        
        error_message = "❌ Une erreur est survenue lors de l'envoi."
        
        if update.message:
            await update.message.reply_text(
                error_message,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text(
                error_message,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            
        return MAIN_MENU


async def handle_send_scheduled_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post: dict):
    """Gère l'envoi d'un post planifié spécifique"""
    query = update.callback_query
    
    try:
        # ✅ VALIDATION AMÉLIORÉE DU CANAL
        channel = post.get('channel_username')
        if not channel:
            # Récupérer depuis la base de données si manquant
            try:
                with sqlite3.connect(settings.db_config["path"]) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT c.username, c.name
                        FROM posts p
                        JOIN channels c ON p.channel_id = c.id
                        WHERE p.id = ?
                    """, (post.get('id'),))
                    result = cursor.fetchone()
                    if result:
                        channel = result[0]
                        if not channel.startswith('@'):
                            channel = f"@{channel}"
                        post['channel_username'] = channel
                        logger.info(f"✅ Canal récupéré depuis la DB: {channel}")
                    else:
                        raise ValueError("Canal introuvable dans la base de données")
            except Exception as e:
                logger.error(f"Erreur récupération canal depuis DB: {e}")
                raise ValueError("Impossible de déterminer le canal de destination")
        
        # Valider le format du canal
        if not channel.startswith('@') and not channel.startswith('-'):
            channel = f"@{channel}"
        
        logger.info(f"📍 Canal validé: {channel}")
        
        # Construire le clavier avec boutons URL si présents
        keyboard = []
        if post.get('buttons'):
            try:
                if isinstance(post['buttons'], str):
                    try:
                        buttons = json.loads(post['buttons'])
                    except json.JSONDecodeError:
                        logger.warning("Impossible de décoder les boutons JSON")
                        buttons = post['buttons']
                else:
                    buttons = post['buttons']
                    
                for btn in buttons:
                    keyboard.append([InlineKeyboardButton(btn['text'], url=btn['url'])])
            except Exception as e:
                logger.error(f"Erreur lors de la conversion des boutons : {e}")

        # Envoyer selon le type
        sent_message = None
        post_type = post.get('type')
        content = post.get('content')
        caption = post.get('caption')
        
        # ✅ VALIDATION DU CONTENU
        if not content:
            raise ValueError("Contenu du post manquant")
            
        if not post_type:
            raise ValueError("Type de post manquant")
            
        keyboard_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        logger.info(f"📤 Envoi vers {channel} - Type: {post_type}")
        
        if post_type == "photo":
            reactions = post.get("reactions", [])
            from mon_bot_telegram.handlers.reaction_functions import create_reactions_keyboard
            reply_markup = create_reactions_keyboard(reactions) if reactions else None

            sent_message = await context.bot.send_photo(
                chat_id=channel,
                photo=content,
                caption=caption,
                reply_markup=reply_markup
            )
        elif post_type == "video":
            reactions = post.get("reactions", [])
            from mon_bot_telegram.handlers.reaction_functions import create_reactions_keyboard
            reply_markup = create_reactions_keyboard(reactions) if reactions else None

            sent_message = await context.bot.send_video(
                chat_id=channel,
                video=content,
                caption=caption,
                reply_markup=reply_markup
            )
        elif post_type == "document":
            reactions = post.get("reactions", [])
            from mon_bot_telegram.handlers.reaction_functions import create_reactions_keyboard
            reply_markup = create_reactions_keyboard(reactions) if reactions else None

            sent_message = await context.bot.send_document(
                chat_id=channel,
                document=content,
                caption=caption,
                reply_markup=reply_markup
            )
        elif post_type == "text":
            reactions = post.get("reactions", [])
            from mon_bot_telegram.handlers.reaction_functions import create_reactions_keyboard
            reply_markup = create_reactions_keyboard(reactions) if reactions else None

            sent_message = await context.bot.send_message(
                chat_id=channel,
                text=content,
                reply_markup=reply_markup
            )
        else:
            raise ValueError(f"Type de post non supporté: {post_type}")

        if sent_message:
            # Supprimer de la base de données
            with sqlite3.connect(settings.db_config["path"]) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM posts WHERE id = ?", (post['id'],))
                conn.commit()

            # Supprimer le job du scheduler
            job_id = f"post_{post['id']}"
            try:
                # Utiliser le scheduler manager au lieu de job_queue
                scheduler_manager = get_scheduler_manager()
                if scheduler_manager:
                    if scheduler_manager.scheduler.get_job(job_id):
                        scheduler_manager.scheduler.remove_job(job_id)
                        logger.info(f"Job {job_id} supprimé du scheduler après envoi")
                else:
                    logger.warning("Scheduler manager non disponible pour suppression après envoi")
            except Exception as e:
                logger.warning(f"Job {job_id} non supprimé du scheduler: {e}")

            await query.edit_message_text(
                f"✅ **Post envoyé avec succès !**\n\n"
                f"📍 Canal : {channel}\n"
                f"📝 Type : {post_type}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]]),
                parse_mode='Markdown'
            )

            # Nettoyer les données
            context.user_data.pop('current_scheduled_post', None)
            logger.info("✅ Post planifié envoyé avec succès")
            return MAIN_MENU
        else:
            raise RuntimeError("Échec de l'envoi du message")

    except Exception as e:
        logger.error(f"❌ Erreur lors de l'envoi du post planifié : {e}")
        await query.edit_message_text(
            f"❌ **Erreur lors de l'envoi**\n\n"
            f"Détails: {str(e)}\n\n"
            f"Vérifiez que le bot est administrateur du canal.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]]),
            parse_mode='Markdown'
        )
        return MAIN_MENU

async def handle_send_normal_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, posts: list):
    """Gère l'envoi de posts normaux (non planifiés)"""
    query = update.callback_query
    
    try:
        # Récupérer les paramètres d'envoi avec validation améliorée
        selected_channel = context.user_data.get('selected_channel', {})
        channel = posts[0].get("channel") or selected_channel.get('username', '@default_channel')
        auto_destruction_time = context.user_data.get('auto_destruction_time', 0)
        
        # ✅ VALIDATION DU CANAL
        if not channel or channel == '@default_channel':
            # Essayer de récupérer un canal depuis la base de données
            user_id = update.effective_user.id
            db_manager = DatabaseManager()
            channels = db_manager.list_channels(user_id)
            if channels:
                channel = channels[0].get('username', '@default_channel')
                if not channel.startswith('@'):
                    channel = f"@{channel}"
            else:
                logger.warning("Aucun canal configuré, utilisation du canal par défaut")
        
        # Valider le format du canal
        if not channel.startswith('@') and not channel.startswith('-'):
            channel = f"@{channel}"
        
        logger.info(f"📍 Envoi vers le canal: {channel}")
        
        sent_count = 0
        for post_index, post in enumerate(posts):
            try:
                post_type = post.get('type')
                content = post.get('content')
                caption = post.get('caption', '')
                
                # ✅ VALIDATION DU CONTENU
                if not content:
                    logger.warning(f"Post {post_index + 1} ignoré: contenu manquant")
                    continue
                    
                if not post_type:
                    logger.warning(f"Post {post_index + 1} ignoré: type manquant")
                    continue
                
                logger.info(f"📤 Envoi du post {post_index + 1}/{len(posts)} - Type: {post_type}")
                
                # ✅ CONSTRUIRE LE CLAVIER AVEC RÉACTIONS ET BOUTONS
                keyboard = []
                
                # DEBUG: Afficher le contenu complet du post
                logger.info(f"🔍 DEBUG POST {post_index + 1}:")
                logger.info(f"   📝 Type: {post.get('type')}")
                logger.info(f"   📄 Content: {str(post.get('content'))[:50]}...")
                logger.info(f"   📝 Caption: {post.get('caption', 'None')}")
                logger.info(f"   ⭐ Réactions: {post.get('reactions', 'None')}")
                logger.info(f"   🔘 Boutons: {post.get('buttons', 'None')}")
                logger.info(f"   🔍 Type réactions: {type(post.get('reactions'))}")
                logger.info(f"   🔍 Type boutons: {type(post.get('buttons'))}")
                
                # Ajouter les réactions en ligne
                reactions = post.get('reactions', [])
                logger.info(f"🎯 Traitement réactions pour post {post_index + 1}: {reactions}")
                
                if reactions:
                    # Si c'est une string JSON, la parser
                    if isinstance(reactions, str):
                        try:
                            reactions = json.loads(reactions)
                            logger.info(f"✅ Réactions parsées depuis JSON: {reactions}")
                        except json.JSONDecodeError:
                            logger.warning(f"❌ Impossible de parser les réactions JSON: {reactions}")
                            reactions = []
                    
                    if reactions:
                        current_row = []
                        for reaction in reactions:
                            logger.info(f"⭐ Ajout réaction: {reaction}")
                            current_row.append(InlineKeyboardButton(
                                reaction,
                                callback_data=f"reaction_{post_index}_{reaction}"
                            ))
                            # 4 réactions par ligne maximum
                            if len(current_row) == 4:
                                keyboard.append(current_row)
                                current_row = []
                        # Ajouter la dernière ligne si elle n'est pas vide
                        if current_row:
                            keyboard.append(current_row)
                
                # Ajouter les boutons URL
                buttons = post.get('buttons', [])
                logger.info(f"🎯 Traitement boutons pour post {post_index + 1}: {buttons}")
                
                if buttons:
                    # Si c'est une string JSON, la parser
                    if isinstance(buttons, str):
                        try:
                            buttons = json.loads(buttons)
                            logger.info(f"✅ Boutons parsés depuis JSON: {buttons}")
                        except json.JSONDecodeError:
                            logger.warning(f"❌ Impossible de parser les boutons JSON: {buttons}")
                            buttons = []
                    
                    for button in buttons:
                        if isinstance(button, dict) and 'text' in button and 'url' in button:
                            logger.info(f"🔘 Ajout bouton: {button['text']} → {button['url']}")
                            keyboard.append([InlineKeyboardButton(button['text'], url=button['url'])])
                
                # Créer le markup si on a des boutons/réactions
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                
                logger.info(f"🎯 Post {post_index + 1} - Réactions: {len(reactions) if isinstance(reactions, list) else 0}, Boutons: {len(buttons) if isinstance(buttons, list) else 0}")
                logger.info(f"🎯 Clavier créé: {len(keyboard)} ligne(s) de boutons")
                logger.info(f"🎯 Reply markup créé: {reply_markup is not None}")
                if reply_markup:
                    logger.info(f"🎯 Contenu du reply_markup: {reply_markup.inline_keyboard}")
                
                sent_message = None
                logger.info(f"📤 Envoi vers {channel} avec reply_markup: {reply_markup is not None}")
                
                if post_type == "photo":
                    sent_message = await context.bot.send_photo(
                        chat_id=channel,
                        photo=content,
                        caption=caption,
                        reply_markup=reply_markup
                    )
                elif post_type == "video":
                    sent_message = await context.bot.send_video(
                        chat_id=channel,
                        video=content,
                        caption=caption,
                        reply_markup=reply_markup
                    )
                elif post_type == "document":
                    sent_message = await context.bot.send_document(
                        chat_id=channel,
                        document=content,
                        caption=caption,
                        reply_markup=reply_markup
                    )
                elif post_type == "text":
                    sent_message = await context.bot.send_message(
                        chat_id=channel,
                        text=content,
                        reply_markup=reply_markup
                    )
                else:
                    logger.warning(f"Type de post non supporté: {post_type}")
                    continue
                
                if sent_message:
                    sent_count += 1
                    logger.info(f"✅ Post {post_index + 1} envoyé avec succès")
                    
                    # Programmer l'auto-destruction si configurée
                    if auto_destruction_time > 0:
                        schedule_auto_destruction(context, channel, sent_message.message_id, auto_destruction_time)
                
            except Exception as e:
                logger.error(f"Erreur envoi post {post_index + 1}: {e}")
                continue

        # Nettoyer les données après envoi
        context.user_data['posts'] = []
        context.user_data.pop('selected_channel', None)
        context.user_data.pop('auto_destruction_time', None)
        
        # Message de confirmation
        success_message = f"✅ **Envoi terminé !**\n\n"
        success_message += f"📊 {sent_count}/{len(posts)} post(s) envoyé(s)\n"
        success_message += f"📍 Canal : {channel}"
        
        if auto_destruction_time > 0:
            if auto_destruction_time < 3600:
                time_str = f"{auto_destruction_time // 60} minute(s)"
            elif auto_destruction_time < 86400:
                time_str = f"{auto_destruction_time // 3600} heure(s)"
            else:
                time_str = f"{auto_destruction_time // 86400} jour(s)"
            success_message += f"\n\n⏰ Auto-destruction : {time_str}"
        
        await query.edit_message_text(
            success_message,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]]),
            parse_mode='Markdown'
        )
        
        logger.info("✅ === FIN handle_send_normal_posts - SUCCÈS ===")
        return MAIN_MENU
        
    except Exception as e:
        logger.error(f"❌ Erreur dans handle_send_normal_posts: {e}")
        await query.edit_message_text(
            f"❌ **Erreur lors de l'envoi**\n\n"
            f"Détails: {str(e)}\n\n"
            f"Vérifiez que le bot est administrateur du canal.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]]),
            parse_mode='Markdown'
        )
        return MAIN_MENU

# Fin du fichier - Ancienne fonction send_post_now dupliquée supprimée
# La fonction send_post_now complète est définie plus haut dans le fichier (ligne 1760)

async def process_thumbnail_and_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, post_index: int) -> bool:
    """
    Fonction centralisée pour traiter l'ajout de thumbnail à un post.
    
    Cette fonction :
    1. Télécharge le fichier original
    2. Applique le thumbnail personnalisé
    3. Re-upload le fichier avec le nouveau thumbnail
    4. Remplace le file_id dans le post
    5. Nettoie les fichiers temporaires
    
    Returns:
        bool: True si succès, False sinon
    """
    query = update.callback_query
    user_id = update.effective_user.id
    temp_files = []  # Pour le nettoyage
    try:
        # 📋 ÉTAPE 1 : RÉCUPÉRER LE POST ET SES INFOS
        logger.info(f"🎯 PROCESS_THUMBNAIL: Début pour post {post_index + 1}")
        
        if 'posts' not in context.user_data or post_index >= len(context.user_data['posts']):
            logger.error("❌ Post introuvable dans le contexte")
            return False
            
        post = context.user_data['posts'][post_index]
        post_type = post.get('type')
        content = post.get('content')  # file_id original
        caption = post.get('caption', '')
        filename = post.get('filename')
        
        # ✅ VALIDATION INITIALE DU POST
        if not post_type or not content:
            logger.error(f"❌ Post invalide: type={post_type}, content={content}")
            await safe_edit_callback_message(
                query,
                "❌ **Post invalide**\n\nLe post ne contient pas d'informations valides.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return False
        
        # Récupérer le canal
        channel_username = post.get('channel', context.user_data.get('selected_channel', {}).get('username'))
        clean_username = normalize_channel_username(channel_username)
        
        if not clean_username:
            logger.error("❌ Impossible de déterminer le canal")
            await safe_edit_callback_message(
                query,
                "❌ **Canal introuvable**\n\nImpossible de déterminer le canal cible.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return False
            
        logger.info(f"📊 Post info: type={post_type}, canal=@{clean_username}")
        
        # 🖼️ ÉTAPE 2 : RÉCUPÉRER LE THUMBNAIL
        db_manager = DatabaseManager()
        thumbnail_data = db_manager.get_thumbnail(clean_username, user_id)
        
        if not thumbnail_data:
            logger.error(f"❌ Aucun thumbnail trouvé pour @{clean_username}")
            await safe_edit_callback_message(
                query,
                f"❌ **Aucun thumbnail enregistré**\n\n"
                f"Aucun thumbnail trouvé pour @{clean_username}.\n"
                "Veuillez d'abord enregistrer un thumbnail via les paramètres.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Paramètres", callback_data="custom_settings"),
                    InlineKeyboardButton("↩️ Retour", callback_data="main_menu")
                ]])
            )
            return False
        
        # Extraire le file_id et le chemin local
        thumbnail_file_id = None
        thumbnail_local_path = None
        
        if isinstance(thumbnail_data, dict):
            thumbnail_file_id = thumbnail_data.get('file_id')
            thumbnail_local_path = thumbnail_data.get('local_path')
        else:
            # Ancien format (juste file_id)
            thumbnail_file_id = thumbnail_data
        
        logger.info(f"🖼️ Thumbnail trouvé: file_id={thumbnail_file_id[:30] if thumbnail_file_id else 'None'}..., local_path={thumbnail_local_path}")
            
        # 📥 ÉTAPE 3 : VALIDATION ET RÉCUPÉRATION DU FICHIER ORIGINAL
        await safe_edit_callback_message(
            query,
            f"⏳ **Traitement du post {post_index + 1}...**\n\n"
            "📥 Validation et récupération du fichier...",
            parse_mode="Markdown"
        )
        
        file_path = None
        
        # ✅ PRIORITÉ 1 : Vérifier le fichier local du post
        local_path = post.get('local_path')
        if local_path:
            logger.info(f"🔍 Vérification fichier local: {local_path}")
            
            # Validation complète du fichier local
            if not os.path.exists(local_path):
                logger.error(f"❌ Fichier local absent: {local_path}")
                await safe_edit_callback_message(
                    query,
                    f"❌ **Fichier original introuvable**\n\n" \
                    f"Le fichier d'origine n'existe plus sur le serveur.\n\n" \
                    f"💡 **Solution :** Renvoyez le fichier au bot pour pouvoir lui appliquer un thumbnail.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]),
                    parse_mode="Markdown"
                )
                return False
                
            if not os.path.isfile(local_path):
                logger.error(f"❌ Le chemin ne pointe pas vers un fichier: {local_path}")
                await safe_edit_callback_message(
                    query,
                    f"❌ **Fichier original invalide**\n\n" \
                    f"Le chemin du fichier n'est pas valide.\n\n" \
                    f"💡 **Solution :** Renvoyez le fichier au bot.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]),
                    parse_mode="Markdown"
                )
                return False
                
            file_size = os.path.getsize(local_path)
            if file_size == 0:
                logger.error(f"❌ Fichier local vide: {local_path}")
                await safe_edit_callback_message(
                    query,
                    f"❌ **Fichier original vide**\n\n" \
                    f"Le fichier d'origine est vide (0 B).\n\n" \
                    f"💡 **Solution :** Renvoyez le fichier au bot.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]),
                    parse_mode="Markdown"
                )
                return False
                
            if not os.access(local_path, os.R_OK):
                logger.error(f"❌ Fichier local non lisible: {local_path}")
                await safe_edit_callback_message(
                    query,
                    f"❌ **Fichier original inaccessible**\n\n" \
                    f"Impossible de lire le fichier (permissions).\n\n" \
                    f"💡 **Solution :** Renvoyez le fichier au bot.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")]]),
                    parse_mode="Markdown"
                )
                return False
                
            logger.info(f"✅ Fichier local validé: {local_path} ({file_size/1024/1024:.1f} MB)")
            file_path = local_path
            
        else:
            # ✅ FALLBACK : Télécharger via file_id avec gestion robuste des erreurs
            logger.warning(f"⚠️ Aucun fichier local pour le post {post_index + 1}, tentative de téléchargement")
            
            await safe_edit_callback_message(
                query,
                f"⏳ **Traitement du post {post_index + 1}...**\n\n"
                "📥 Téléchargement du fichier original...",
                parse_mode="Markdown"
            )
            
            try:
                # Essayer d'abord avec l'API Bot
                logger.info(f"📥 Tentative téléchargement API Bot: {content[:30]}...")
                file_obj = await context.bot.get_file(content)
                file_path = await file_obj.download_to_drive()
                
                if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                    raise Exception("Fichier téléchargé invalide via API Bot")
                    
                temp_files.append(file_path)
                logger.info(f"✅ Fichier téléchargé via API Bot: {file_path}")
                
            except Exception as e:
                error_str = str(e)
                logger.warning(f"⚠️ Erreur API Bot: {error_str}")
                
                # Fallback vers clients avancés pour FILE_REFERENCE_EXPIRED ou fichier trop gros
                if ("File is too big" in error_str or "file is too big" in error_str.lower() or 
                    "FILE_REFERENCE_EXPIRED" in error_str or "file reference" in error_str.lower()):
                    
                    try:
                        from utils.clients import client_manager
                        client_info = await client_manager.get_best_client(100*1024*1024, "download")
                        client = client_info["client"]
                        client_type = client_info["type"]
                        
                        logger.info(f"📥 Téléchargement via {client_type} pour fichier problématique")
                        
                        import time
                        if client_type == "pyrogram":
                            file_path = await client.download_media(
                                content,
                                file_name=f"temp_{user_id}_{int(time.time())}"
                            )
                        elif client_type == "telethon":
                            file_path = await client.download_media(
                                content,
                                file=f"temp_{user_id}_{int(time.time())}"
                            )
                        else:
                            raise Exception("Aucun client avancé disponible")
                            
                        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                            raise Exception("Fichier téléchargé invalide via client avancé")
                            
                        temp_files.append(file_path)
                        logger.info(f"✅ Téléchargement client avancé réussi: {file_path}")
                        
                    except Exception as client_error:
                        logger.error(f"❌ Échec avec client avancé: {client_error}")
                        await safe_edit_callback_message(
                            query,
                            f"❌ **Impossible de récupérer le fichier**\n\n"
                            f"Le fichier n'est plus accessible (file_id expiré ou fichier trop gros).\n\n"
                            f"💡 **Solution** : Renvoyez le fichier au bot pour pouvoir lui ajouter un thumbnail.\n\n"
                            f"**Détails :** {client_error}",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                            ]]),
                            parse_mode="Markdown"
                        )
                        return False
                else:
                    logger.error(f"❌ Erreur inconnue lors du téléchargement: {error_str}")
                    await safe_edit_callback_message(
                        query,
                        f"❌ **Erreur de téléchargement**\n\n"
                        f"Impossible de récupérer le fichier.\n\n"
                        f"💡 **Solution** : Renvoyez le fichier au bot.\n\n"
                        f"**Erreur :** {error_str}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                        ]]),
                        parse_mode="Markdown"
                    )
                    return False
        
        # ✅ VALIDATION FINALE DU FICHIER RÉCUPÉRÉ
        if not file_path or not os.path.exists(file_path):
            logger.error("❌ Aucun fichier valide récupéré")
            await safe_edit_callback_message(
                query,
                f"❌ **Fichier introuvable**\n\n"
                f"Impossible de localiser le fichier à traiter.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]])
            )
            return False
            
        final_file_size = os.path.getsize(file_path)
        if final_file_size == 0:
            logger.error(f"❌ Fichier final vide: {file_path}")
            await safe_edit_callback_message(
                query,
                f"❌ **Fichier vide détecté**\n\n"
                f"Le fichier récupéré est vide (0 B).\n\n"
                f"💡 **Solution :** Renvoyez le fichier au bot.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                ]]),
                parse_mode="Markdown"
            )
            return False
            
        logger.info(f"✅ Fichier final validé: {file_path} ({final_file_size/1024/1024:.1f} MB)")
        
        # 📤 ÉTAPE 4 : ENVOI DIRECT AVEC THUMBNAIL
        await safe_edit_callback_message(
            query,
            f"⏳ **Traitement du post {post_index + 1}...**\n\n"
            "📤 Application du thumbnail et envoi...",
            parse_mode="Markdown"
        )
        
        # Utiliser le fichier local en priorité, sinon le file_id
        thumb_to_use = thumbnail_local_path if thumbnail_local_path and os.path.exists(thumbnail_local_path) else thumbnail_file_id
        
        # Utiliser send_file_smart qui gère déjà les thumbnails et la validation
        from .media_handler import send_file_smart
        
        result = await send_file_smart(
            chat_id=update.effective_user.id,  # Envoyer vers l'utilisateur
            file_path=file_path,
            caption=caption,
            thumb_id=thumb_to_use,
            file_name=filename,
            force_document=(post_type == "document"),
            context=context
        )
        
        if result["success"]:
            # Récupérer le message_id pour supprimer le message temporaire
            message_id = result.get("message_id")
            if message_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_user.id,
                        message_id=message_id
                    )
                    logger.info("✅ Message temporaire supprimé")
                except Exception as delete_error:
                    logger.warning(f"⚠️ Impossible de supprimer le message temporaire: {delete_error}")
                    
            # Le nouveau file_id est dans le résultat
            new_file_id = result.get("file_id")
            if new_file_id:
                logger.info(f"✅ Nouveau file_id obtenu: {new_file_id[:30]}...")
                
                # 🔄 ÉTAPE 5 : REMPLACER LE FILE_ID
                post['content'] = new_file_id
                post['has_custom_thumbnail'] = True
                post['original_file_id'] = content  # Garder l'ancien au cas où
                
                logger.info(f"✅ Post {post_index + 1} mis à jour avec thumbnail personnalisé")
                
                # Supprimer les messages précédents (comme pour le bouton Rename)
                try:
                    # Supprimer le message de demande de thumbnail si possible
                    if 'thumbnail_prompt_message_id' in context.user_data:
                        try:
                            await context.bot.delete_message(
                                chat_id=update.effective_chat.id,
                                message_id=context.user_data['thumbnail_prompt_message_id']
                            )
                        except Exception as e:
                            logger.warning(f"Impossible de supprimer le message de demande: {e}")
                        context.user_data.pop('thumbnail_prompt_message_id', None)
                    
                    # Supprimer le message du fichier original (si on a son ID)
                    if 'original_file_message_id' in context.user_data:
                        try:
                            await context.bot.delete_message(
                                chat_id=update.effective_chat.id,
                                message_id=context.user_data['original_file_message_id']
                            )
                        except Exception as e:
                            logger.warning(f"Impossible de supprimer le fichier original: {e}")
                        context.user_data.pop('original_file_message_id', None)
                    
                except Exception as e:
                    logger.warning(f"Erreur lors de la suppression des messages: {e}")
                
                # Envoyer le fichier avec le thumbnail et les boutons
                try:
                    # Créer les boutons comme dans les autres fonctions
                    buttons = []
                    
                    # Bouton pour ajouter des réactions
                    if not post.get('reactions'):
                        buttons.append([InlineKeyboardButton("✨ Ajouter des réactions", callback_data=f"add_reactions_{post_index}")])
                    else:
                        buttons.append([InlineKeyboardButton("🗑️ Supprimer les réactions", callback_data=f"remove_reactions_{post_index}")])
                    
                    # Bouton pour ajouter un bouton URL
                    if not post.get('buttons'):
                        buttons.append([InlineKeyboardButton("🔗 Ajouter un bouton URL", callback_data=f"add_url_button_{post_index}")])
                    else:
                        buttons.append([InlineKeyboardButton("🗑️ Supprimer le bouton URL", callback_data=f"remove_url_{post_index}")])
                    
                    # Boutons d'édition et suppression
                    buttons.append([
                        InlineKeyboardButton("✏️ Edit File", callback_data=f"edit_file_{post_index}"),
                        InlineKeyboardButton("❌ Supprimer", callback_data=f"delete_post_{post_index}")
                    ])
                    

                    
                    reply_markup = InlineKeyboardMarkup(buttons)
                    
                    # Envoyer le fichier avec le nouveau thumbnail
                    if post_type == 'photo':
                        await context.bot.send_photo(
                            chat_id=update.effective_user.id,
                            photo=new_file_id,
                            caption=caption,
                            reply_markup=reply_markup
                        )
                    elif post_type == 'video':
                        await context.bot.send_video(
                            chat_id=update.effective_user.id,
                            video=new_file_id,
                            caption=caption,
                            reply_markup=reply_markup
                        )
                    elif post_type == 'document':
                        await context.bot.send_document(
                            chat_id=update.effective_user.id,
                            document=new_file_id,
                            caption=caption,
                            reply_markup=reply_markup
                        )
                    elif post_type == 'text':
                        await context.bot.send_message(
                            chat_id=update.effective_user.id,
                            text=caption,
                            reply_markup=reply_markup
                        )
                    
                    logger.info(f"✅ Fichier avec thumbnail envoyé avec boutons")
                    
                except Exception as e:
                    logger.error(f"Erreur lors de l'envoi du fichier avec thumbnail: {e}")
                    # En cas d'erreur, envoyer un message de succès simple
                    await safe_edit_callback_message(
                        query,
                        f"✅ **Thumbnail appliqué avec succès !**\n\n"
                        f"📝 Post {post_index + 1} ({post_type})\n"
                        f"🖼️ Thumbnail personnalisé pour @{clean_username}\n\n"
                        f"Le fichier est maintenant prêt à être envoyé avec son thumbnail personnalisé.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🚀 Envoyer maintenant", callback_data="send_now"),
                            InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
                        ]]),
                        parse_mode="Markdown"
                    )
                
                return True
            else:
                raise Exception("Aucun file_id retourné par send_file_smart")
        else:
            error_detail = result.get('error', 'Erreur inconnue')
            raise Exception(f"Échec de send_file_smart: {error_detail}")
                
    except Exception as e:
        logger.error(f"❌ Erreur lors du traitement: {e}")
        await safe_edit_callback_message(
            query,
            f"❌ **Erreur lors du traitement**\n\n"
            f"Impossible d'appliquer le thumbnail au post {post_index + 1}.\n\n"
            f"**Erreur :** {str(e)}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Réessayer", callback_data=f"add_thumbnail_{post_index}"),
                InlineKeyboardButton("↩️ Menu principal", callback_data="main_menu")
            ]]),
            parse_mode="Markdown"
        )
        return False
            
    finally:
        # 🧹 ÉTAPE 6 : NETTOYAGE (déplacé dans finally)
        # Ne supprimer que les fichiers temporaires, pas les fichiers locaux permanents
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    logger.info(f"🧹 Fichier temporaire supprimé: {temp_file}")
            except Exception as cleanup_error:
                logger.warning(f"⚠️ Erreur nettoyage: {cleanup_error}")
        
        # Note: Les fichiers local_path ne sont PAS supprimés car ils sont permanents

