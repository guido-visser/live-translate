# The window shown while the first start (or the first start after an update) installs the
# software the app runs on. It runs before Python exists on this PC, so it uses what Windows has.
# Started hidden by LiveTranslate.vbs. Exit codes: 0 = done and the app was started, 10 = nothing
# more to do (closed by the user, or already open), anything else = LiveTranslate.vbs falls back
# to LiveTranslate.cmd in a console, which does the same work. Save this file as UTF-8 with BOM:
# Windows PowerShell 5.1 reads it as ANSI otherwise, and the Cyrillic below breaks.
#
# -Move: started by the app (Settings -> About), which then closes. The software can't simply be
# moved along, because Python environments contain absolute paths, so it copies the data, sets the
# software up again from the copied download cache, and only then switches over and removes the old
# copy. Until that last step nothing has changed: closing or a failure leaves everything as it was.
param([string]$AppArgs = "", [switch]$Move)

$ErrorActionPreference = "Stop"
$app = $PSScriptRoot
# Always here: the settings and the note saying where the big data went.
$base = Join-Path $env:LOCALAPPDATA "LiveTranslate"
$locationFile = Join-Path $base "data-location.txt"

# The admin rights of an elevated start shouldn't end up in the user's own data folder.
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process explorer.exe -ArgumentList "`"$app\LiveTranslate.vbs`""
    exit 10
}

$created = $false
$mutex = New-Object Threading.Mutex($true, "Local\LiveTranslate-Setup", [ref]$created)
if (-not $created) {
    (New-Object -ComObject WScript.Shell).AppActivate("Live Translate") | Out-Null
    exit 10
}

New-Item -ItemType Directory -Force $base | Out-Null
$uv = Join-Path $app "bin\uv.exe"
$version = "dev"
if (Test-Path "$app\VERSION") { $version = (Get-Content "$app\VERSION" -TotalCount 1).Trim() }

# Only a first start asks: an existing installation keeps its data where it is.
$data = $base
$choosing = -not (Test-Path (Join-Path $base "venv"))
$unreachable = ""
$removed = ""
if (Test-Path $locationFile) {
    $data = [IO.File]::ReadAllText($locationFile).Trim()
    $choosing = $false
    if (-not (Test-Path ([IO.Path]::GetPathRoot($data)))) { $choosing = $true; $unreachable = $data }
    # Removed by hand. A setup that was stopped halfway has left its downloads there, and carries on.
    elseif (@(Get-ChildItem -LiteralPath $data -Force -ErrorAction SilentlyContinue).Count -eq 0) { $choosing = $true; $removed = $data }
}
$moving = [bool]$Move
$moveFrom = $data
if ($moving) { $choosing = $true }
# These stay in the standard folder whatever happens, so the app can always find its way.
$AlwaysInBase = "settings.json", "settings.tmp", "data-location.txt"

function Use-DataFolder($folder) {
    # Keep in step with LiveTranslate.cmd.
    New-Item -ItemType Directory -Force $folder | Out-Null
    $script:data = $folder
    $env:LIVETRANSLATE_DATA = $folder
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $folder "venv"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $folder "python"
    $env:UV_PYTHON_PREFERENCE = "only-managed"
    $env:UV_CACHE_DIR = Join-Path $folder "uv-cache"
    $env:UV_HTTP_TIMEOUT = "300"
    $script:marker = Join-Path $env:UV_PROJECT_ENVIRONMENT ".installed-$version"
    $script:log = Join-Path $folder "setup.log"
    # An update only has to change a few packages, and how long that takes can't be told beforehand.
    $script:updating = @(Get-ChildItem $env:UV_PROJECT_ENVIRONMENT -Filter ".installed-*" -Force -ErrorAction SilentlyContinue).Count -gt 0
}

# What uv writes during a complete first start: Python and the packages, unpacked in its cache.
# Installing from the cache is quick and only links those files, so this is what the bar shows.
$ExpectedBytes = 2.43GB
# Software, speech and translation models and voices, as Explorer adds them up; needs some room to spare.
$NeededBytes = 8GB

$language = "en"
if (Test-Path "$app\LANGUAGE") { $language = (Get-Content "$app\LANGUAGE" -TotalCount 1).Trim() }
$Texts = @{
    en = @{
        heading = "Initial setup"; update = "Updating Live Translate"
        preparing = "Getting ready..."; python = "Downloading Python..."
        packages = "Downloading the speech recognition software..."; installing = "Installing the software..."
        starting = "Starting Live Translate..."
        once = "This only happens once and can take a while, depending on your internet connection. You can keep using this PC in the meantime."
        onceUpdate = "This takes about a minute, and Live Translate then starts by itself."
        of = "{0} of about {1}"; failed = "Setup could not finish"
        failedHelp = "This is often caused by a lost internet connection. Check the connection and try again; what was downloaded so far is kept."
        retry = "Try again"; close = "Close"; details = "Show details"
        leaveTitle = "Stop the setup?"; leave = "Live Translate is still being set up. If you stop now, it continues where it left off the next time you start the app."
        where = "Where should Live Translate store its data?"
        whereHelp = "This is a large set of data: the software and the speech and translation models take up about 7 GB. It grows with extra languages and with service logs, especially if they record the sound. You can choose another drive; it has to be connected whenever you use Live Translate."
        folder = "Folder"; browse = "Browse..."; next = "Continue"; free = "Free on {0}: {1}"
        tooSmall = "There is too little room on {0}: {1} free, and about 8 GB is needed. Choose another drive."
        notUsable = "This folder can't be used: {0}"; network = "A network drive isn't suitable. Choose a drive in this PC."
        notWritable = "Live Translate may not write there. Choose another folder."; noLinks = "This drive doesn't support what the software needs (it has to be NTFS). Choose another drive."
        badPath = "That isn't a complete folder path, like D:\LiveTranslate."
        gone = "The data folder {0} can't be reached. Connect that drive and start Live Translate again, or choose another folder: everything is then downloaded again."
        removed = "The data folder {0} is empty or no longer exists, so everything has to be downloaded again. Choose where it should go."
        pick = "Choose a folder for the Live Translate data"
        moveHeading = "Move data folder"
        moveWhere = "Where should the data go?"
        moveHelp = "It is now in {0}. Choose an empty folder or a new one; another drive is fine, as long as it's connected whenever you use Live Translate. Everything is copied there first, and the old folder is only removed once it all works."
        moveStart = "Move"
        cancel = "Cancel"
        waiting = "Waiting for Live Translate to close..."
        copying = "Copying your data..."
        moveInstalling = "Setting up the software in the new folder..."
        cleaning = "Removing the old folder..."
        moveOnce = "This can take a few minutes. Your data stays in the old folder until everything is ready."
        moveFailed = "Moving could not finish. Nothing has changed: Live Translate keeps using the old folder. Details are in the log."
        notEmpty = "This folder isn't empty. Choose an empty folder or a new one."
        nested = "The new folder can't be inside the current one, or the other way around."
        needRoom = "There is too little room on {0}: {1} free, and about {2} is needed. Choose another drive."
    }
    nl = @{
        heading = "Eerste installatie"; update = "Live Translate bijwerken"
        preparing = "Voorbereiden..."; python = "Python downloaden..."
        packages = "De software voor spraakherkenning downloaden..."; installing = "De software installeren..."
        starting = "Live Translate starten..."
        once = "Dit gebeurt maar één keer en kan even duren, afhankelijk van je internetverbinding. Je kunt deze pc ondertussen gewoon gebruiken."
        onceUpdate = "Dit duurt ongeveer een minuut, daarna start Live Translate vanzelf."
        of = "{0} van ongeveer {1}"; failed = "De installatie kon niet worden afgerond"
        failedHelp = "Dit komt vaak door een weggevallen internetverbinding. Controleer de verbinding en probeer het opnieuw; wat al gedownload is, blijft bewaard."
        retry = "Opnieuw proberen"; close = "Sluiten"; details = "Details tonen"
        leaveTitle = "Installatie stoppen?"; leave = "Live Translate wordt nog geïnstalleerd. Als je nu stopt, gaat het verder waar het gebleven was zodra je de app weer start."
        where = "Waar mag Live Translate zijn gegevens bewaren?"
        whereHelp = "Dit is een grote hoeveelheid data: de software en de spraak- en vertaalmodellen nemen ongeveer 7 GB in beslag. Dat groeit met extra talen en met dienstlogs, vooral als die ook het geluid opnemen. Je kunt een andere schijf kiezen; die moet aangesloten zijn wanneer je Live Translate gebruikt."
        folder = "Map"; browse = "Bladeren..."; next = "Doorgaan"; free = "Vrij op {0}: {1}"
        tooSmall = "Er is te weinig ruimte op {0}: {1} vrij, en er is ongeveer 8 GB nodig. Kies een andere schijf."
        notUsable = "Deze map kan niet worden gebruikt: {0}"; network = "Een netwerkschijf is niet geschikt. Kies een schijf in deze pc."
        notWritable = "Live Translate mag daar niet schrijven. Kies een andere map."; noLinks = "Deze schijf ondersteunt niet wat de software nodig heeft (hij moet NTFS zijn). Kies een andere schijf."
        badPath = "Dat is geen volledig pad naar een map, zoals D:\LiveTranslate."
        gone = "De gegevensmap {0} is niet bereikbaar. Sluit die schijf aan en start Live Translate opnieuw, of kies een andere map: dan wordt alles opnieuw gedownload."
        removed = "De gegevensmap {0} is leeg of bestaat niet meer, dus alles moet opnieuw worden gedownload. Kies waar het naartoe moet."
        pick = "Kies een map voor de gegevens van Live Translate"
        moveHeading = "Gegevensmap verplaatsen"
        moveWhere = "Waar moeten de gegevens naartoe?"
        moveHelp = "Ze staan nu in {0}. Kies een lege of nieuwe map; een andere schijf mag ook, als die aangesloten is wanneer je Live Translate gebruikt. Alles wordt eerst daarheen gekopieerd, en de oude map wordt pas verwijderd als alles werkt."
        moveStart = "Verplaatsen"
        cancel = "Annuleren"
        waiting = "Wachten tot Live Translate gesloten is..."
        copying = "Je gegevens kopiëren..."
        moveInstalling = "De software in de nieuwe map klaarzetten..."
        cleaning = "De oude map verwijderen..."
        moveOnce = "Dit kan een paar minuten duren. Je gegevens blijven in de oude map staan tot alles klaar is."
        moveFailed = "Het verplaatsen kon niet worden afgerond. Er is niets veranderd: Live Translate blijft de oude map gebruiken. De details staan in het logboek."
        notEmpty = "Deze map is niet leeg. Kies een lege of nieuwe map."
        nested = "De nieuwe map mag niet in de huidige staan, of andersom."
        needRoom = "Er is te weinig ruimte op {0}: {1} vrij, en er is ongeveer {2} nodig. Kies een andere schijf."
    }
    ru = @{
        heading = "Первоначальная настройка"; update = "Обновление Live Translate"
        preparing = "Подготовка..."; python = "Загрузка Python..."
        packages = "Загрузка программ для распознавания речи..."; installing = "Установка программ..."
        starting = "Запуск Live Translate..."
        once = "Это происходит только один раз и может занять некоторое время в зависимости от скорости интернета. Тем временем можно пользоваться компьютером."
        onceUpdate = "Это займёт около минуты, затем Live Translate запустится сам."
        of = "{0} из примерно {1}"; failed = "Не удалось завершить настройку"
        failedHelp = "Часто причина в потере связи с интернетом. Проверьте подключение и повторите попытку; уже загруженное сохранится."
        retry = "Повторить"; close = "Закрыть"; details = "Подробности"
        leaveTitle = "Остановить настройку?"; leave = "Live Translate ещё настраивается. Если остановить сейчас, настройка продолжится с того же места при следующем запуске."
        where = "Где Live Translate будет хранить данные?"
        whereHelp = "Это большой объём данных: программы и модели речи и перевода занимают около 7 ГБ. Объём растёт с добавлением языков и журналов служб, особенно если записывается звук. Можно выбрать другой диск; он должен быть подключён при использовании Live Translate."
        folder = "Папка"; browse = "Обзор..."; next = "Продолжить"; free = "Свободно на {0}: {1}"
        tooSmall = "На {0} слишком мало места: свободно {1}, а нужно около 8 ГБ. Выберите другой диск."
        notUsable = "Эту папку нельзя использовать: {0}"; network = "Сетевой диск не подходит. Выберите диск этого компьютера."
        notWritable = "Live Translate не может записывать туда. Выберите другую папку."; noLinks = "Этот диск не поддерживает то, что нужно программам (нужна файловая система NTFS). Выберите другой диск."
        badPath = "Это не полный путь к папке, например D:\LiveTranslate."
        gone = "Папка данных {0} недоступна. Подключите этот диск и снова запустите Live Translate или выберите другую папку: тогда всё будет загружено заново."
        removed = "Папка данных {0} пуста или больше не существует, поэтому всё нужно загрузить заново. Выберите, куда сохранить данные."
        pick = "Выберите папку для данных Live Translate"
        moveHeading = "Перемещение папки данных"
        moveWhere = "Куда переместить данные?"
        moveHelp = "Сейчас они находятся в {0}. Выберите пустую или новую папку; можно на другом диске, если он подключён при использовании Live Translate. Сначала всё копируется туда, а старая папка удаляется, только когда всё работает."
        moveStart = "Переместить"
        cancel = "Отмена"
        waiting = "Ожидание закрытия Live Translate..."
        copying = "Копирование данных..."
        moveInstalling = "Подготовка программ в новой папке..."
        cleaning = "Удаление старой папки..."
        moveOnce = "Это может занять несколько минут. Данные остаются в старой папке, пока всё не будет готово."
        moveFailed = "Не удалось завершить перемещение. Ничего не изменилось: Live Translate продолжает использовать старую папку. Подробности в журнале."
        notEmpty = "Эта папка не пуста. Выберите пустую или новую папку."
        nested = "Новая папка не может находиться внутри текущей, и наоборот."
        needRoom = "На {0} слишком мало места: свободно {1}, а нужно около {2}. Выберите другой диск."
    }
    uk = @{
        heading = "Початкове налаштування"; update = "Оновлення Live Translate"
        preparing = "Підготовка..."; python = "Завантаження Python..."
        packages = "Завантаження програм для розпізнавання мовлення..."; installing = "Встановлення програм..."
        starting = "Запуск Live Translate..."
        once = "Це відбувається лише один раз і може тривати якийсь час залежно від швидкості інтернету. Тим часом можна користуватися комп'ютером."
        onceUpdate = "Це триватиме близько хвилини, потім Live Translate запуститься сам."
        of = "{0} із приблизно {1}"; failed = "Не вдалося завершити налаштування"
        failedHelp = "Часто причина у втраті зв'язку з інтернетом. Перевірте підключення і спробуйте ще раз; уже завантажене збережеться."
        retry = "Спробувати ще раз"; close = "Закрити"; details = "Подробиці"
        leaveTitle = "Зупинити налаштування?"; leave = "Live Translate ще налаштовується. Якщо зупинити зараз, налаштування продовжиться з того ж місця під час наступного запуску."
        where = "Де Live Translate зберігатиме дані?"
        whereHelp = "Це великий обсяг даних: програми та моделі мовлення й перекладу займають близько 7 ГБ. Обсяг зростає з додаванням мов і журналів служінь, особливо якщо записується звук. Можна обрати інший диск; він має бути підключений під час використання Live Translate."
        folder = "Папка"; browse = "Огляд..."; next = "Продовжити"; free = "Вільно на {0}: {1}"
        tooSmall = "На {0} замало місця: вільно {1}, а потрібно близько 8 ГБ. Оберіть інший диск."
        notUsable = "Цю папку не можна використати: {0}"; network = "Мережевий диск не підходить. Оберіть диск цього комп'ютера."
        notWritable = "Live Translate не може записувати туди. Оберіть іншу папку."; noLinks = "Цей диск не підтримує те, що потрібно програмам (потрібна файлова система NTFS). Оберіть інший диск."
        badPath = "Це не повний шлях до папки, наприклад D:\LiveTranslate."
        gone = "Папка даних {0} недоступна. Підключіть цей диск і знову запустіть Live Translate або оберіть іншу папку: тоді все буде завантажено заново."
        removed = "Папка даних {0} порожня або більше не існує, тому все потрібно завантажити знову. Оберіть, куди зберегти дані."
        pick = "Оберіть папку для даних Live Translate"
        moveHeading = "Переміщення папки даних"
        moveWhere = "Куди перемістити дані?"
        moveHelp = "Зараз вони в {0}. Оберіть порожню або нову папку; можна на іншому диску, якщо він підключений під час використання Live Translate. Спочатку все копіюється туди, а стара папка видаляється, лише коли все працює."
        moveStart = "Перемістити"
        cancel = "Скасувати"
        waiting = "Очікування закриття Live Translate..."
        copying = "Копіювання даних..."
        moveInstalling = "Підготовка програм у новій папці..."
        cleaning = "Видалення старої папки..."
        moveOnce = "Це може тривати кілька хвилин. Дані залишаються в старій папці, доки все не буде готово."
        moveFailed = "Не вдалося завершити переміщення. Нічого не змінилося: Live Translate продовжує використовувати стару папку. Подробиці в журналі."
        notEmpty = "Ця папка не порожня. Оберіть порожню або нову папку."
        nested = "Нова папка не може бути всередині поточної, і навпаки."
        needRoom = "На {0} замало місця: вільно {1}, а потрібно близько {2}. Оберіть інший диск."
    }
}
$T = $Texts[$language]
if (-not $T) { $T = $Texts.en }

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

# The same colours as the operator page, in light or dark like Windows itself.
$dark = $false
try { $dark = (Get-ItemPropertyValue "HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" AppsUseLightTheme) -eq 0 } catch {}
if ($dark) { $bg = "#1B1F26"; $text = "#ECEEF2"; $muted = "#8E94A0"; $track = "#2D333D"; $accent = "#6C9BFF"; $accentText = "#0B1020"; $danger = "#FF7B7B" }
else { $bg = "#FFFFFF"; $text = "#1C1E22"; $muted = "#6C707A"; $track = "#E8E6E0"; $accent = "#2A6DF4"; $accentText = "#FFFFFF"; $danger = "#D23B3B" }

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Live Translate" Width="560" SizeToContent="Height" ResizeMode="CanMinimize"
        WindowStartupLocation="CenterScreen" Background="$bg" Foreground="$text"
        FontFamily="Segoe UI" FontSize="14" TextOptions.TextFormattingMode="Display">
  <Window.Resources>
    <Style TargetType="ProgressBar">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ProgressBar">
            <Border x:Name="PART_Track" CornerRadius="5" Background="$track" ClipToBounds="True">
              <Grid HorizontalAlignment="Left">
                <Border x:Name="PART_Indicator" CornerRadius="5" Background="$accent" HorizontalAlignment="Left"/>
              </Grid>
            </Border>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="Button">
      <Setter Property="Padding" Value="16,7"/>
      <Setter Property="Margin" Value="0,0,10,0"/>
      <Setter Property="Background" Value="$track"/>
      <Setter Property="Foreground" Value="$text"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border CornerRadius="8" Background="{TemplateBinding Background}" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>
  <StackPanel Margin="32,28,32,28">
    <StackPanel Orientation="Horizontal" Margin="0,0,0,18">
      <Image x:Name="Logo" Width="40" Height="40" Margin="0,0,14,0"/>
      <StackPanel VerticalAlignment="Center">
        <TextBlock Text="Live Translate" Foreground="$muted" FontSize="13"/>
        <TextBlock x:Name="Heading" FontSize="22" FontWeight="SemiBold"/>
      </StackPanel>
    </StackPanel>
    <StackPanel x:Name="Choosing" Visibility="Collapsed">
      <TextBlock x:Name="Where" FontSize="15" FontWeight="SemiBold" Margin="0,0,0,8" TextWrapping="Wrap"/>
      <TextBlock x:Name="WhereHelp" TextWrapping="Wrap" Margin="0,0,0,16"/>
      <TextBlock x:Name="FolderLabel" Foreground="$muted" FontSize="13" Margin="0,0,0,4"/>
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBox x:Name="FolderBox" Grid.Column="0" Padding="8,7" Margin="0,0,10,0" VerticalContentAlignment="Center"
                 Background="$track" Foreground="$text" CaretBrush="$text" BorderThickness="0"/>
        <Button x:Name="Browse" Grid.Column="1" Margin="0"/>
      </Grid>
      <TextBlock x:Name="Free" Foreground="$muted" FontSize="13" Margin="0,6,0,0" MinHeight="18"/>
      <TextBlock x:Name="ChooseError" Foreground="$danger" TextWrapping="Wrap" Margin="0,6,0,0" Visibility="Collapsed"/>
      <StackPanel Orientation="Horizontal" Margin="0,16,0,0">
        <Button x:Name="ChooseNext" Background="$accent" Foreground="$accentText" FontWeight="SemiBold"/>
        <Button x:Name="ChooseCancel" Visibility="Collapsed"/>
      </StackPanel>
    </StackPanel>
    <StackPanel x:Name="Working">
      <TextBlock x:Name="Stage" FontSize="15" Margin="0,0,0,10" TextWrapping="Wrap"/>
      <ProgressBar x:Name="Bar" Height="10" Minimum="0" Maximum="100"/>
      <TextBlock x:Name="Amount" Foreground="$muted" FontSize="13" Margin="0,8,0,0" MinHeight="18"/>
      <TextBlock x:Name="Once" Foreground="$muted" FontSize="13" Margin="0,14,0,0" TextWrapping="Wrap"/>
    </StackPanel>
    <StackPanel x:Name="Failed" Visibility="Collapsed">
      <TextBlock x:Name="FailedTitle" Foreground="$danger" FontSize="15" FontWeight="SemiBold" Margin="0,0,0,8" TextWrapping="Wrap"/>
      <TextBlock x:Name="FailedHelp" TextWrapping="Wrap" Margin="0,0,0,18"/>
      <StackPanel Orientation="Horizontal">
        <Button x:Name="Retry" Background="$accent" Foreground="$accentText" FontWeight="SemiBold"/>
        <Button x:Name="Details"/>
        <Button x:Name="CloseButton"/>
      </StackPanel>
    </StackPanel>
  </StackPanel>
</Window>
"@
$window = [Windows.Markup.XamlReader]::Load((New-Object Xml.XmlNodeReader $xaml))
function Named($name) { $window.FindName($name) }
$bar = Named "Bar"; $stage = Named "Stage"; $amount = Named "Amount"
(Named "Heading").Text = $T.heading
(Named "Where").Text = $T.where
(Named "WhereHelp").Text = $T.whereHelp
(Named "FolderLabel").Text = $T.folder
(Named "Browse").Content = $T.browse
(Named "ChooseNext").Content = $T.next
(Named "ChooseCancel").Content = $T.cancel
(Named "FailedTitle").Text = $T.failed
(Named "FailedHelp").Text = $T.failedHelp
(Named "Retry").Content = $T.retry
(Named "Details").Content = $T.details
(Named "CloseButton").Content = $T.close
$icon = New-Object Windows.Media.Imaging.BitmapImage
$icon.BeginInit(); $icon.UriSource = New-Object Uri("$app\LiveTranslate.ico"); $icon.EndInit()
$window.Icon = $icon
(Named "Logo").Source = $icon
$stage.Text = $T.preparing

$shared = [hashtable]::Synchronized(@{ bytes = 0L; stop = $false; folders = @(); installer = 0; earlier = 0L })

function Start-Counting {
    # Counting can take a moment on a slow disk, so that happens beside the window.
    $counter = [powershell]::Create()
    [void]$counter.AddScript({
        param($shared)
        while (-not $shared.stop) {
            if ($shared.folders.Count -gt 0) {
                $total = 0L
                foreach ($folder in $shared.folders) {
                    if ([IO.Directory]::Exists($folder)) {
                        try {
                            foreach ($file in [IO.Directory]::EnumerateFiles($folder, "*", [IO.SearchOption]::AllDirectories)) {
                                try { $total += ([IO.FileInfo]$file).Length } catch {}
                            }
                        } catch {}
                    }
                }
                $shared.bytes = $total
                Start-Sleep -Seconds 2
                continue
            }
            # Never look through uv's folders while it runs: it can't rename a folder that is being
            # read ("Access is denied"). Windows keeps count of what each program writes anyway.
            try {
                $running = @(Get-CimInstance Win32_Process -Filter "Name='uv.exe' AND ParentProcessId=$($shared.installer)")
                if ($running.Count -gt 0) { $shared.bytes = $shared.earlier + [long]$running[0].WriteTransferCount }
            } catch {}
            Start-Sleep -Milliseconds 500
        }
    }).AddArgument($shared)
    [void]$counter.BeginInvoke()
}

$script:process = $null
$script:attempt = 0
$script:phase = if ($choosing) { "choosing" } else { "syncing" }
$script:startedApp = $null
$script:exitCode = 10
$script:shown = 0.0

function Start-Sync {
    $script:attempt++
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = "$env:ComSpec"
    $info.Arguments = "/c `"`"$uv`" sync --frozen --no-dev --no-editable --color never --project `"$app\.`" >> `"$log`" 2>&1`""
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $shared.earlier = $shared.bytes  # what an attempt before this one already wrote
    $script:process = [Diagnostics.Process]::Start($info)
    $shared.installer = $script:process.Id
}

function Begin-Setup {
    (Named "Choosing").Visibility = "Collapsed"
    (Named "Working").Visibility = "Visible"
    (Named "Heading").Text = if ($updating) { $T.update } else { $T.heading }
    (Named "Once").Text = if ($updating) { $T.onceUpdate } else { $T.once }
    $bar.IsIndeterminate = $updating
    $script:phase = "syncing"
    Remove-Item $log -ErrorAction SilentlyContinue
    Start-Counting
    Start-Sync
    $timer.Start()
}

# -- moving the data folder ---------------------------------------------------------------------

$script:moveTarget = $null
$script:moveStep = "copy"
$script:moveBytes = 1L
$script:copies = @()
$script:moveDecided = $false

function Get-Bytes($folder, [string[]]$skip = @()) {
    $total = 0L
    if (-not (Test-Path -LiteralPath $folder)) { return 0L }
    foreach ($item in Get-ChildItem -LiteralPath $folder -Force -ErrorAction SilentlyContinue) {
        if ($skip -contains $item.Name) { continue }
        if (-not $item.PSIsContainer) { $total += $item.Length; continue }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        try {
            foreach ($file in [IO.Directory]::EnumerateFiles($item.FullName, "*", [IO.SearchOption]::AllDirectories)) {
                try { $total += ([IO.FileInfo]$file).Length } catch {}
            }
        } catch {}
    }
    return $total
}

# Before a folder was picked, LibreTranslate's models lived in the home folder; see config.py.
function Get-HomeModels {
    $models = Join-Path $env:USERPROFILE ".local\share\argos-translate"
    if (-not (Test-Path $locationFile) -and (Test-Path -LiteralPath $models)) { return $models }
    return $null
}

function Test-MoveTarget($target) {
    $from = $moveFrom.TrimEnd('\')
    if ($target.StartsWith("$from\", [StringComparison]::OrdinalIgnoreCase) -or $from.StartsWith("$target\", [StringComparison]::OrdinalIgnoreCase)) { return $T.nested }
    if (Test-Path -LiteralPath $target) {
        $others = @(Get-ChildItem -LiteralPath $target -Force | Where-Object { $AlwaysInBase -notcontains $_.Name })
        if ($others.Count -gt 0) { return $T.notEmpty }
    }
    $skip = "venv", "libretranslate-env", "setup.log"
    $script:moveBytes = [Math]::Max(1L, (Get-Bytes $moveFrom ($skip + $AlwaysInBase)))
    $models = Get-HomeModels
    if ($models) { $script:moveBytes += Get-Bytes $models }
    # Plus the software that gets set up again there, and some room to spare.
    $needed = $script:moveBytes + 4GB
    $root = [IO.Path]::GetPathRoot($target)
    $drive = New-Object IO.DriveInfo($root)
    if ($drive.IsReady -and $drive.AvailableFreeSpace -lt $needed) { return $T.needRoom -f $root, (Format-Size $drive.AvailableFreeSpace), (Format-Size $needed) }
    return $null
}

function Start-Copying {
    $script:moveStep = "copy"
    $script:phase = "waiting"
    $script:startedApp = Get-Date
    $stage.Text = $T.waiting
    $bar.IsIndeterminate = $true
    $amount.Text = ""
    $script:copies = @(, @($moveFrom, $script:moveTarget))
    $models = Get-HomeModels
    if ($models) { $script:copies += , @($models, (Join-Path $script:moveTarget "shared\argos-translate")) }
    $timer.Start()
}

function Start-Move($target) {
    $script:moveTarget = $target
    Use-DataFolder $target
    $script:updating = $true  # how long setting up takes can't be told beforehand
    (Named "Choosing").Visibility = "Collapsed"
    (Named "Working").Visibility = "Visible"
    (Named "FailedHelp").Text = $T.moveFailed
    (Named "Once").Text = $T.moveOnce
    $shared.folders = @($target)
    Start-Counting
    Start-Copying
}

function Test-AppGone {
    if (Test-AppIsUp) { return $false }
    $prefix = $moveFrom.TrimEnd('\') + "\"
    foreach ($running in Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name LIKE 'libretranslate%'") {
        if ($running.ExecutablePath -and $running.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    return $true
}

function Start-NextCopy {
    $from, $to = $script:copies[0]
    $script:copies = @($script:copies | Select-Object -Skip 1)
    $exclude = ""
    if ($from -eq $moveFrom) {
        # /XJ: uv's folder link for Python points at the old place, and it makes a new one.
        $exclude = "/XD `"$moveFrom\venv`" `"$moveFrom\libretranslate-env`" /XF setup.log " + (($AlwaysInBase | ForEach-Object { $_ }) -join " ")
    }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = "robocopy.exe"
    $info.Arguments = "`"$from`" `"$to`" /E /XJ /R:3 /W:2 /NFL /NDL /NP /NJH /NJS $exclude /LOG+:`"$log`""
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $script:process = [Diagnostics.Process]::Start($info)
}

function On-MoveTick {
    if ($script:phase -eq "waiting") {
        if (-not (Test-AppGone)) {
            if (((Get-Date) - $script:startedApp).TotalSeconds -gt 90) { Show-Failure }
            return
        }
        $script:phase = "copying"
        $stage.Text = $T.copying
        $bar.IsIndeterminate = $false
        Start-NextCopy
        return
    }
    if ($script:phase -eq "copying") {
        $share = [Math]::Min(1.0, $shared.bytes / $script:moveBytes)
        $bar.Value = $share * 100
        $amount.Text = $T.of -f (Format-Size ($share * $script:moveBytes)), (Format-Size $script:moveBytes)
        if (-not $script:process.HasExited) { return }
        if ($script:process.ExitCode -ge 8) { Show-Failure; return }  # below 8 robocopy succeeded
        if ($script:copies.Count -gt 0) { Start-NextCopy; return }
        $script:moveStep = "sync"
        $shared.folders = @()  # uv is about to work in there
        $script:phase = "syncing"
        $script:attempt = 0
        $bar.IsIndeterminate = $true
        $amount.Text = ""
        $stage.Text = $T.moveInstalling
        Start-Sync
        return
    }
    # "cleaning": the old copy is going away beside the window.
    if ($script:cleaner.IsCompleted) { Start-TheApp }
}

# Deleting with rmdir: unlike PowerShell's Remove-Item it never follows a folder link.
function Start-Removing([string[]]$paths, $folderToDrop) {
    $worker = [powershell]::Create()
    [void]$worker.AddScript({
        param($paths, $folderToDrop)
        foreach ($path in $paths) {
            if ([IO.Directory]::Exists($path)) { & $env:ComSpec /c "rmdir /s /q `"$path`"" 2>$null }
            elseif ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
        }
        # Only when empty. Something that just ended can hold on to the folder for a moment.
        for ($try = 0; $folderToDrop -and $try -lt 10 -and [IO.Directory]::Exists($folderToDrop); $try++) {
            try { [IO.Directory]::Delete($folderToDrop) } catch { Start-Sleep -Milliseconds 500 }
        }
    }).AddArgument($paths).AddArgument($folderToDrop)
    return $worker.BeginInvoke()
}

function Get-DataItems($folder) {
    @(Get-ChildItem -LiteralPath $folder -Force -ErrorAction SilentlyContinue | Where-Object { $AlwaysInBase -notcontains $_.Name } | ForEach-Object { $_.FullName })
}

function Finish-Move {
    $models = Get-HomeModels
    [IO.File]::WriteAllText($locationFile, $script:moveTarget, [Text.Encoding]::Unicode)
    $script:moveDecided = $true
    $old = Get-DataItems $moveFrom
    if ($models) { $old += $models }
    $drop = if ($moveFrom -ieq $base) { $null } else { $moveFrom }
    $script:phase = "cleaning"
    $stage.Text = $T.cleaning
    $script:cleaner = Start-Removing $old $drop
}

# Closed or failed: back to how it was, and the app starts again from the old folder.
function Stop-Move {
    $script:moveDecided = $true
    if ($script:moveTarget) {
        $removing = Start-Removing (Get-DataItems $script:moveTarget) $(if ($script:moveTarget -ieq $base) { $null } else { $script:moveTarget })
        [void]$removing.AsyncWaitHandle.WaitOne(120000)  # this process ends right after, and the work with it
    }
    Remove-Item Env:LIVETRANSLATE_DATA -ErrorAction SilentlyContinue
    Start-Process wscript.exe -ArgumentList "`"$app\LiveTranslate.vbs`""
}

# "D:\" or "D:\Kerk" become a folder of our own in there, which keeps uninstalling and tidying simple.
function Get-TargetFolder($typed) {
    $typed = $typed.Trim().Trim('"')
    if ($typed -notmatch '^[A-Za-z]:\\' -and $typed -notmatch '^\\\\') { return $null }
    $full = [IO.Path]::GetFullPath($typed).TrimEnd('\')
    if ((Split-Path $full -Leaf) -eq "LiveTranslate") { return $full }
    return Join-Path ($full + "\") "LiveTranslate"
}

# Returns what is wrong with this folder, or nothing. It really tries, because a drive can say it's
# fine and still refuse what the install needs: uv makes a folder link for its Python.
function Test-DataFolder($folder) {
    if (-not $folder) { return $T.badPath }
    if ($folder.StartsWith("\\")) { return $T.network }
    $root = [IO.Path]::GetPathRoot($folder)
    $drive = New-Object IO.DriveInfo($root)
    if (-not $drive.IsReady) { return $T.notUsable -f $root }
    if ($drive.DriveType -eq "Network") { return $T.network }
    if ($drive.AvailableFreeSpace -lt $NeededBytes) { return $T.tooSmall -f $root, (Format-Size $drive.AvailableFreeSpace) }
    $existed = Test-Path $folder
    $probe = Join-Path $folder ".livetranslate-probe"
    try {
        New-Item -ItemType Directory -Force $probe | Out-Null
        [IO.File]::WriteAllText((Join-Path $probe "test.txt"), "test")
    } catch {
        return $T.notWritable
    }
    try {
        $link = Join-Path $probe "link"
        & $env:ComSpec /c "mklink /J `"$link`" `"$probe`" >nul 2>&1"
        if ($LASTEXITCODE -ne 0) { return $T.noLinks }
        [IO.Directory]::Delete($link)
    } finally {
        Remove-Item -Recurse -Force $probe -ErrorAction SilentlyContinue
        if (-not $existed) { Remove-Item $folder -ErrorAction SilentlyContinue }
    }
    return $null
}

# The dialog Explorer users know, with the address bar and search. .NET Framework only offers the old
# folder tree, so this asks Windows for it directly.
function Select-Folder($owner, $title, $start) {
    if (-not ("LiveTranslateFolderDialog" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class LiveTranslateFolderDialog {
    [ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")] class FileOpenDialog {}

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItem {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem parent);
        void GetDisplayName(uint sigdn, [MarshalAs(UnmanagedType.LPWStr)] out string name);
        void GetAttributes(uint mask, out uint attributes);
        void Compare(IShellItem other, uint hint, out int order);
    }

    [ComImport, Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IFileDialog {
        [PreserveSig] int Show(IntPtr parent);
        void SetFileTypes(uint count, IntPtr types);
        void SetFileTypeIndex(uint index);
        void GetFileTypeIndex(out uint index);
        void Advise(IntPtr events, out uint cookie);
        void Unadvise(uint cookie);
        void SetOptions(uint options);
        void GetOptions(out uint options);
        void SetDefaultFolder(IShellItem item);
        void SetFolder(IShellItem item);
        void GetFolder(out IShellItem item);
        void GetCurrentSelection(out IShellItem item);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
        void GetResult(out IShellItem item);
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, ref Guid riid, out IShellItem item);

    const uint PickFolders = 0x20, FileSystemOnly = 0x40;
    const uint FileSystemPath = 0x80058000;
    const int Cancelled = unchecked((int)0x800704C7);

    // The folder chosen, or null when cancelled.
    public static string Show(IntPtr owner, string title, string start) {
        var dialog = (IFileDialog)new FileOpenDialog();
        uint options;
        dialog.GetOptions(out options);
        dialog.SetOptions(options | PickFolders | FileSystemOnly);
        dialog.SetTitle(title);
        if (!string.IsNullOrEmpty(start)) {
            var iid = typeof(IShellItem).GUID;
            IShellItem folder;
            SHCreateItemFromParsingName(start, IntPtr.Zero, ref iid, out folder);
            dialog.SetFolder(folder);
        }
        int result = dialog.Show(owner);
        if (result == Cancelled) return null;
        if (result != 0) Marshal.ThrowExceptionForHR(result);
        IShellItem item;
        dialog.GetResult(out item);
        string path;
        item.GetDisplayName(FileSystemPath, out path);
        return path;
    }
}
"@
    }
    return [LiveTranslateFolderDialog]::Show($owner, $title, $start)
}

function Show-FreeSpace {
    $free = ""
    try {
        $folder = Get-TargetFolder (Named "FolderBox").Text
        if ($folder -and -not $folder.StartsWith("\\")) {
            $root = [IO.Path]::GetPathRoot($folder)
            $drive = New-Object IO.DriveInfo($root)
            if ($drive.IsReady) { $free = $T.free -f $root, (Format-Size $drive.AvailableFreeSpace) }
        }
    } catch {}
    (Named "Free").Text = $free
}

function Show-ChooseError($message) {
    (Named "ChooseError").Text = $message
    (Named "ChooseError").Visibility = if ($message) { "Visible" } else { "Collapsed" }
}

function Show-Failure {
    $timer.Stop()
    (Named "Working").Visibility = "Collapsed"
    (Named "Failed").Visibility = "Visible"
}

function Read-Stage {
    # uv says what it does in plain lines, and only the big files get a line of their own.
    if (-not (Test-Path $log)) { return $T.preparing }
    $tail = (Get-Content $log -Tail 40 -Encoding UTF8 -ErrorAction SilentlyContinue) -join "`n"
    if ($tail -match "(?m)^\s*(Prepared|Installed) ") { return $T.installing }
    if ($tail -match "(?m)^\s*Downloading (?!cpython)") { return $T.packages }
    if ($tail -match "(?m)^\s*Using CPython") { return $T.packages }
    if ($tail -match "cpython") { return $T.python }
    return $T.preparing
}

function Format-Size($bytes) {
    if ($bytes -ge 1GB) { return "{0:0.0} GB" -f ($bytes / 1GB) }
    return "{0:0} MB" -f ($bytes / 1MB)
}

function Move-Bar($percent) {
    # The count arrives in steps; the bar glides to it in the time until the next one.
    $glide = New-Object Windows.Media.Animation.DoubleAnimation($percent, [Windows.Duration]$timer.Interval)
    $bar.BeginAnimation([Windows.Controls.ProgressBar]::ValueProperty, $glide)
}

function Test-AppIsUp {
    $port = 8000
    try { $port = (Get-Content (Join-Path $base "settings.json") -Raw -Encoding UTF8 | ConvertFrom-Json).port } catch {}
    $client = New-Object Net.Sockets.TcpClient
    try {
        $client.BeginConnect("127.0.0.1", $port, $null, $null).AsyncWaitHandle.WaitOne(200) | Out-Null
        return $client.Connected
    } catch { return $false } finally { $client.Close() }
}

function Start-TheApp {
    $script:phase = "starting"
    $script:exitCode = 0
    $script:startedApp = Get-Date
    $bar.IsIndeterminate = $false
    $bar.BeginAnimation([Windows.Controls.ProgressBar]::ValueProperty, $null)  # or it holds on to the last value
    $bar.Value = 100
    $amount.Text = ""
    $stage.Text = $T.starting
    Start-Process -WindowStyle Hidden -FilePath "$env:ComSpec" -ArgumentList "/c `"`"$app\LiveTranslate.cmd`" $AppArgs`""
}

function On-Tick {
    if ($script:phase -eq "starting") {
        # Stays up until the app can open its own window, so the screen is never empty.
        if ((Test-AppIsUp) -or ((Get-Date) - $script:startedApp).TotalSeconds -gt 90) { $window.Close() }
        return
    }
    if ($script:phase -in "waiting", "copying", "cleaning") { On-MoveTick; return }
    if (-not $updating) {
        $script:shown = [Math]::Min(0.98, $shared.bytes / $ExpectedBytes)
        Move-Bar ($script:shown * 100)
        # Only MB: the text would change once per 100 MB in GB.
        $amount.Text = $T.of -f ("{0:N0} MB" -f ($script:shown * $ExpectedBytes / 1MB)), ("{0:N0} MB" -f ($ExpectedBytes / 1MB))
    }
    if (-not $moving) { $stage.Text = Read-Stage }
    if (-not $script:process.HasExited) { return }
    if ($script:process.ExitCode -ne 0) {
        if ($script:attempt -lt 2) { Start-Sync } else { Show-Failure }
        return
    }
    Set-Content -Path $marker -Value "ok" -Encoding ASCII
    if ($moving) { Finish-Move } else { Start-TheApp }
}

$timer = New-Object Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(700)
$timer.Add_Tick({ try { On-Tick } catch { Add-Content $log "setup window: $_"; Show-Failure } })

(Named "Retry").Add_Click({
    (Named "Failed").Visibility = "Collapsed"
    (Named "Working").Visibility = "Visible"
    $stage.Text = $T.preparing
    $script:attempt = 0
    Remove-Item $log -ErrorAction SilentlyContinue
    if ($moving -and $script:moveStep -eq "copy") { Start-Copying; return }
    Start-Sync
    $timer.Start()
})
(Named "Details").Add_Click({ Start-Process notepad.exe -ArgumentList "`"$log`"" })
(Named "CloseButton").Add_Click({ $window.Close() })
(Named "ChooseCancel").Add_Click({ $window.Close() })
(Named "FolderBox").Add_TextChanged({ Show-FreeSpace; Show-ChooseError "" })
(Named "Browse").Add_Click({
    # Opens where the folder is now, or as near to it as still exists.
    $start = $null
    try { $start = (Named "FolderBox").Text.Trim() } catch {}
    while ($start -and -not (Test-Path -LiteralPath $start -PathType Container)) { $start = Split-Path $start -Parent }
    $owner = (New-Object Windows.Interop.WindowInteropHelper($window)).Handle
    $picked = Select-Folder $owner $T.pick $start
    if ($picked) { (Named "FolderBox").Text = Get-TargetFolder $picked }
})
(Named "ChooseNext").Add_Click({
    $folder = $null
    try {
        $folder = Get-TargetFolder (Named "FolderBox").Text
        if ($moving -and $folder -ieq $moveFrom.TrimEnd('\')) { $window.Close(); return }  # nothing to do
        $problem = Test-DataFolder $folder
        if ($moving -and -not $problem) { $problem = Test-MoveTarget $folder }
    } catch {  # e.g. a drive letter that doesn't exist
        Add-Content (Join-Path $base "setup-location.log") "$folder`: $_" -ErrorAction SilentlyContinue
        $problem = $T.notUsable -f (Named "FolderBox").Text
    }
    if ($problem) { Show-ChooseError $problem; return }
    if ($moving) { Start-Move $folder; return }
    Use-DataFolder $folder
    # UTF-16: LiveTranslate.vbs reads it too, and that can't read UTF-8. Any folder name survives this.
    [IO.File]::WriteAllText($locationFile, $folder, [Text.Encoding]::Unicode)
    Begin-Setup
})

$window.Add_Closing({
    param($sender, $e)
    if ($script:phase -eq "cleaning") { $e.Cancel = $true; return }  # past the point of no return, and nearly done
    $working = $script:phase -in "syncing", "copying" -and $null -ne $script:process -and -not $script:process.HasExited
    if ($working) {
        $choice = [Windows.MessageBox]::Show($window, $T.leave, $T.leaveTitle, "YesNo", "Question", "No")
        if ($choice -ne "Yes") { $e.Cancel = $true; return }
        & taskkill.exe /T /F /PID $script:process.Id 2>&1 | Out-Null
    }
    # Cancelled, failed or stopped halfway: the old folder is still the one in use.
    if ($moving -and $script:phase -ne "starting" -and -not $script:moveDecided) { Stop-Move }
})

if ($moving) {
    (Named "Heading").Text = $T.moveHeading
    (Named "Where").Text = $T.moveWhere
    (Named "WhereHelp").Text = $T.moveHelp -f $moveFrom
    (Named "ChooseNext").Content = $T.moveStart
    (Named "ChooseCancel").Visibility = "Visible"
    (Named "Once").Text = $T.moveOnce
}
if ($choosing) {
    (Named "Working").Visibility = "Collapsed"
    (Named "Choosing").Visibility = "Visible"
    (Named "FolderBox").Text = if ($moving) { $moveFrom } elseif ($unreachable) { $unreachable } elseif ($removed) { $removed } else { $base }
    if ($unreachable) { Show-ChooseError ($T.gone -f $unreachable) }
    if ($removed) { Show-ChooseError ($T.removed -f $removed) }
} else {
    Use-DataFolder $data
    $window.Add_ContentRendered({ Begin-Setup })
}
[void]$window.ShowDialog()
$timer.Stop()
$shared.stop = $true
exit $script:exitCode
