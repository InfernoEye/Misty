#include <QAction>
#include <QGuiApplication>
#include <QKeySequence>
#include <QSocketNotifier>
#include <QString>
#include <QTextStream>

#include <KGlobalAccel>

#include <cstdlib>

namespace {

// Stdout protocol consumed by VoiceController._read_helper_output:
//   "READY <PortableText>" once the chord is really registered, or
//   "ERROR <code>" right before exiting non-zero. PRESS/RELEASE follow.
[[noreturn]] void failWithCode(int code)
{
    QTextStream out(stdout);
    out << "ERROR " << code << "\n";
    out.flush();
    QTextStream(stdout).flush();
    ::exit(code);
}

} // namespace

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);

    QCoreApplication::setApplicationName("Misty");
    QCoreApplication::setOrganizationName("Misty");

    QAction holdToTalk;

    holdToTalk.setObjectName("holdToTalk");
    holdToTalk.setText("Hold to talk");
    holdToTalk.setAutoRepeat(false);

    auto *globalAccel = KGlobalAccel::self();

    QString shortcutText = qEnvironmentVariable("MISTY_SHORTCUT");
    if (argc > 1 && argv[1] != nullptr && argv[1][0] != '\0') {
        shortcutText = QString::fromUtf8(argv[1]);
    }
    shortcutText = shortcutText.trimmed();
    const bool explicitShortcut = !shortcutText.isEmpty();
    if (!explicitShortcut) {
        shortcutText = QStringLiteral("F8");
    }

    QKeySequence configuredShortcut(shortcutText);
    const QString canonicalShortcut = configuredShortcut.toString(
        QKeySequence::PortableText
    );
    if (shortcutText.contains(QLatin1Char(','))
        || configuredShortcut.count() > 1) {
        QTextStream(stderr)
            << "KDE global shortcuts support one chord, not a sequence: "
            << shortcutText << "\n";
        failWithCode(4);
    }
    if (configuredShortcut.isEmpty()
        || canonicalShortcut.isEmpty()
        || canonicalShortcut.compare(shortcutText, Qt::CaseInsensitive) != 0) {
        if (explicitShortcut) {
            QTextStream(stderr)
                << "Invalid Misty shortcut (not falling back to F8): "
                << shortcutText << "\n";
            failWithCode(2);
        }
        shortcutText = QStringLiteral("F8");
        configuredShortcut = QKeySequence(shortcutText);
    }

    const QList<QKeySequence> defaultKeys{configuredShortcut};

    if (!globalAccel->setDefaultShortcut(&holdToTalk, defaultKeys)) {
        QTextStream(stderr) << "Could not register Misty's default shortcut.\n";
        failWithCode(1);
    }

    const auto loadFlag = explicitShortcut
        ? KGlobalAccel::NoAutoloading
        : KGlobalAccel::Autoloading;
    if (!globalAccel->setShortcut(&holdToTalk, defaultKeys, loadFlag)) {
        QTextStream(stderr) << "Could not register Misty's shortcut.\n";
        failWithCode(1);
    }

    const QList<QKeySequence> activeKeys =
        globalAccel->shortcut(&holdToTalk);
    if (activeKeys.isEmpty()
        || activeKeys.first().toString(QKeySequence::PortableText)
            .compare(configuredShortcut.toString(
                QKeySequence::PortableText), Qt::CaseInsensitive) != 0) {
        QTextStream(stderr)
            << "KDE did not activate the requested Misty shortcut: "
            << shortcutText << "\n";
        failWithCode(3);
    }

    QAction cancelTask;
    cancelTask.setObjectName("cancelTask");
    cancelTask.setText("Cancel Misty task");
    cancelTask.setAutoRepeat(false);

    const QKeySequence cancelKey(QKeySequence::fromString(QStringLiteral("Esc")));

    QObject::connect(
        globalAccel,
        &KGlobalAccel::globalShortcutActiveChanged,
        &app,
        [&holdToTalk, &cancelTask](QAction *action, bool active) {
            QTextStream output(stdout);
            if (action == &holdToTalk) {
                output << (active ? "PRESS\n" : "RELEASE\n");
            } else if (action == &cancelTask && active) {
                output << "CANCEL\n";
            } else {
                return;
            }
            output.flush();
        }
    );

    // Single READY line announces the chord that KDE actually bound.
    QTextStream ready(stdout);
    ready << "READY "
          << activeKeys.first().toString(QKeySequence::PortableText)
          << "\n";
    ready.flush();

    // Escape is bound only while a task runs, so other applications keep it
    // the rest of the time.
    QTextStream input(stdin);
    QSocketNotifier commands(0, QSocketNotifier::Read);
    QObject::connect(&commands, &QSocketNotifier::activated, [&globalAccel, &cancelTask, &input, cancelKey](int) {
        while (!input.atEnd()) {
            const QString command = input.readLine().trimmed();
            if (command == QLatin1String("CANCEL_ON")) {
                globalAccel->setShortcut(&cancelTask, {cancelKey}, KGlobalAccel::NoAutoloading);
            } else if (command == QLatin1String("CANCEL_OFF")) {
                globalAccel->setShortcut(&cancelTask, {}, KGlobalAccel::NoAutoloading);
            }
        }
    });

    return app.exec();
}
