import discord
import json
import gemini
from datetime import datetime
import os
from pytz import timezone
import io
import db

TOKEN = os.getenv('DISCORD_TOKEN')

client = discord.Client(intents=discord.Intents.all())

@client.event
async def on_ready():
    print('Bot is ready')
    db.init_db()

@client.event
async def on_message(message):
    # print(f'{message.channel}: {message.author}: {message.author.name}: {message.content}')
    if message.author == client.user:
        return
    if message.author.bot:
        return
    dm = (type(message.channel) == discord.DMChannel) and (client.user == message.channel.me)

    if dm or message.content.startswith('!ev'):
        # 以降はGeminiを用いるので、制限を確認
        user_id = str(message.author.id)
        allowed, error_message = db.check_limits(user_id)
        if not allowed:
            await message.channel.send(error_message)
            return
        # メッセージに画像が添付されている場合は初めの一枚を取得
        image = None
        if message.attachments != None:
            for attachment in message.attachments:
                # print("attachment")
                # print(attachment.content_type)
                if attachment.content_type.startswith('image/'):
                    image = await attachment.read()
                    break

        d = datetime.now()
        input = f"""次のイベントの内容を解釈し、日時、タイトル、説明文、開催場所等の情報を生成し、JSON形式で返してください。
出力はJSON文のみとし、1日ごとにイベントを区切り、"events"キーの配列に1つずつ"start_time"、"end_time"、"title"、"description"、"external"、"location"を含んだJSONオブジェクトを格納する形にしてください。
イベントが1つだけでも要素1の配列にし、イベントが存在しない場合は空の配列にすること。
descriptionは箇条書きで簡潔にまとめてください。ただし配列にせず、改行コードを含めた文字列で記述してください。
ただし、プロンプトで与えられた日時は日本標準時(協定世界時-9時間の時差)ですが、start_timeとend_timeは「%Y-%m-%dT%H:%M:%SZ」形式のUTCで書いてください。
end_timeが不明な場合はstart_timeから1時間後の日時を入れてください。

現在の日本標準時での日時は{d.strftime('%Y/%m/%d %H:%M:%S')}です。
開催日時が明示的に過去である場合を除いて、start_timeは現在時刻よりも後の日時を想定しています。したがって、start_time、end_timeが現在日時よりも過去の場合のみ、1年後など現在時刻よりも後の日時を設定してください。
なお、同じ月でも現在日時よりあとの日付の場合は、今年のデータとしてください。
また、開催場所は、明示的にdiscordのボイスチャンネルが貼られた場合は"external"をfalseにして"location"にチャンネルURLを文字列で格納、それ以外の場合は"external"にtrueを入れて"location"にもっともらしい場所の名前やURLの文字列（完全に不明なら「不明」）を格納してください。"""
        if message.reference != None:
            reference = await message.channel.fetch_message(message.reference.message_id)
            input += f"\n\n返信先のメッセージ送信者：{reference.author.name}\n返信先のメッセージ：「{reference.content}」\n\n次がメッセージ本文です。返信先に対する指示がある場合、それに従ってください。"
            # 画像がまだ設定されておらず返信先のメッセージに画像が添付されている場合は初めの一枚を取得
            if image == None and reference.attachments:
                for attachment in reference.attachments:
                    if attachment.content_type.startswith('image/'):
                        image = await attachment.read()
                        break
        input += f"""\nメッセージの送信者：{message.author.name}
イベントについて記述したメッセージ：「{message.content.replace('!ev','').strip()}」"""
        response = str.strip(gemini.getResponse(input))

        # responseを解釈して、日付、タイトル、説明文を取り出す
        if response.startswith("```"):
            response = str.strip(response[3:-3])
        if response.startswith("json"):
            response = str.strip(response[4:])

        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            await message.channel.send("返答のパースに失敗しました：\n" + response)
            return

        # イベントがないまたはサイズ0の場合は警告を出す
        if 'events' not in parsed or len(parsed['events']) == 0:
            await message.channel.send("イベントが見つかりませんでした。")
            return

        responseMessage = "以下のイベントを登録しました。\n"
        ical_text = ""

        try:
            # イベントを1つずつ取り出してdiscordのイベントとして登録
            for event in parsed['events']:
                # UTCでの日時
                start_time = datetime.strptime(event['start_time'], "%Y-%m-%dT%H:%M:%S%z")
                end_time = datetime.strptime(event['end_time'], "%Y-%m-%dT%H:%M:%S%z")
                title = event['title']
                description = event['description']
                external = event['external']
                if external:
                    entity_type = discord.EntityType.external
                    location = event['location']  # 任意の場所
                    channel = None
                    if not dm: # DMの場合はイベントを作成出来ないので登録を無視
                        if image != None:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                        else:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only)
                else:
                    entity_type = discord.EntityType.voice
                    location = None
                    event['location'] = str.strip(event['location'])
                    if event['location'][-1]=="/":
                        event['location'] = event['location'][:-1]
                    event['location'] = event['location'].split('/')[-1]
                    # print(event['location'])
                    channel = message.guild.get_channel(int(event['location']))
                    if not dm: # DMの場合はイベントを作成出来ないので登録を無視
                        if image != None:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                        else:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only)

                # icalendar形式で出力
                ical_text += "BEGIN:VEVENT\n"
                ical_text += f"SUMMARY:{title}\n"
                description_replaced = description.replace('\r', '').replace('\n', '\\n')
                ical_text += f"DESCRIPTION:{description_replaced}\n"
                ical_text += f"DTSTART:{start_time.strftime('%Y%m%dT%H%M%SZ')}\n"
                ical_text += f"DTEND:{end_time.strftime('%Y%m%dT%H%M%SZ')}\n"
                if external:
                    ical_text += f"LOCATION:{location}\n"
                else:
                    ical_text += f"LOCATION:Discord Voice Channel\n"
                ical_text += "END:VEVENT\n"

        except Exception as e:
            await message.channel.send("エラーが発生しました。Botの管理者に連絡してください。")
            # await message.channel.send("エラーが発生しました。Botの管理者に連絡してください。\n" + response + "\n" + str(e))
            return

        if dm: # DMの場合はイベントを作成出来ないので登録を無視
            responseMessage = "以下の内容のスケジュールファイルを作成しました。\n"
        else:
            responseMessage = "以下のイベントを登録しました。\n"
        for event in parsed['events']:
            start_time = datetime.strptime(event['start_time'], "%Y-%m-%dT%H:%M:%S%z")
            end_time = datetime.strptime(event['end_time'], "%Y-%m-%dT%H:%M:%S%z")
            # 日本時間に変換してログに追加
            responseMessage += f"```タイトル：{event['title']}\n説明：{event['description']}\n開始（日本時間）：{start_time.astimezone(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M')}\n終了（日本時間）：{end_time.astimezone(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M')}\n場所：{event['location']}```\n\n"

        # ical_textをメモリ上のファイルに一時保存してアップロード
        with io.StringIO() as f:
            f.write("BEGIN:VCALENDAR\nVERSION:2.0\n")
            f.write(ical_text)
            f.write("END:VCALENDAR\n")
            f.seek(0)  # ファイルポインタを先頭に戻す
            await message.channel.send(responseMessage, file=discord.File(fp=f, filename="event.ics"))

client.run(TOKEN)