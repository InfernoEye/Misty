#include <QAction>
#include <QGuiApplication>
#include <QKeySequence>
#include <QString>
#include <QTextStream>

#include <KGlobalAccel>

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
    if (configuredShortcut.isEmpty()
        || canonicalShortcut.isEmpty()
        || canonicalShortcut.compare(shortcutText, Qt::CaseInsensitive) != 0) {
        if (explicitShortcut) {
            QTextStream(stderr)
                << "Invalid Misty shortcut (not falling back to F8): "
                << shortcutText << "\n";
            return 2;
        }
        shortcutText = QStringLiteral("F8");
        configuredShortcut = QKeySequence(shortcutText);
    }

    const QList<QKeySequence> defaultKeys{configuredShortcut};

    if (!globalAccel->setDefaultShortcut(&holdToTalk, defaultKeys)) {
        QTextStream(stderr) << "Could not register Misty's default shortcut.\n";
        return 1;
    }

    const auto loadFlag = explicitShortcut
        ? KGlobalAccel::NoAutoloading
        : KGlobalAccel::Autoloading;
    if (!globalAccel->setShortcut(&holdToTalk, defaultKeys, loadFlag)) {
        QTextStream(stderr) << "Could not register Misty's shortcut.\n";
        return 1;
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
        return 3;
    }

    QObject::connect(
        globalAccel,
        &KGlobalAccel::globalShortcutActiveChanged,
        &app,
        [&holdToTalk](QAction *action, bool active) {
            if (action != &holdToTalk)
                return;

            QTextStream output(stdout);
            output << (active ? "PRESS\n" : "RELEASE\n");
            output.flush();
        }
    );

    QTextStream(stdout)
        << "Misty shortcut helper ready ("
        << activeKeys.first().toString(QKeySequence::PortableText)
        << ").\n";
    QTextStream(stdout).flush();

    return app.exec();
}
