/*
OBS Avatar Live
Copyright (C) 2026 OBS Avatar Live contributors

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>
*/

// All widgets live on the Qt GUI thread. The dock never receives callbacks
// from IPC threads: a GUI-thread QTimer polls thread-safe snapshots
// (EngineController::status()) from the source registry, and buttons call
// non-blocking controller methods that only enqueue work.

#include "avatar_dock.hpp"
#include "engine_controller.hpp"
#include "source_registry.hpp"

#include <obs-frontend-api.h>
#include <obs-module.h>

#include <QComboBox>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPushButton>
#include <QTimer>
#include <QVBoxLayout>
#include <QWidget>

#include <cstdio>

#define T_(key) obs_module_text(key)

namespace avatar {

namespace {

constexpr const char *kDockId = "avatar_live_control";

QString num(const json::Value &v, const char *suffix = "", int decimals = 1)
{
	if (!v.is_number())
		return QStringLiteral("-");
	return QString::number(v.as_number(), 'f', decimals) + QString::fromUtf8(suffix);
}

class AvatarDock : public QWidget {
public:
	explicit AvatarDock(QWidget *parent = nullptr) : QWidget(parent)
	{
		auto *root = new QVBoxLayout(this);

		source_combo_ = new QComboBox(this);
		root->addWidget(source_combo_);

		auto *buttons = new QHBoxLayout();
		start_ = new QPushButton(QString::fromUtf8(T_("Control.Start")), this);
		stop_ = new QPushButton(QString::fromUtf8(T_("Control.Stop")), this);
		pause_ = new QPushButton(QString::fromUtf8(T_("Control.Pause")), this);
		freeze_ = new QPushButton(QString::fromUtf8(T_("Control.Freeze")), this);
		buttons->addWidget(start_);
		buttons->addWidget(stop_);
		buttons->addWidget(pause_);
		buttons->addWidget(freeze_);
		root->addLayout(buttons);

		preset_ = new QComboBox(this);
		const char *ids[] = {"auto", "cpu_only", "intel_optimized", "max_performance", "low_power"};
		const char *keys[] = {"UserProfile.Auto", "UserProfile.CpuOnly", "UserProfile.IntelOptimized",
				      "UserProfile.MaxPerformance", "UserProfile.LowPower"};
		for (int i = 0; i < 5; i++)
			preset_->addItem(QString::fromUtf8(T_(keys[i])), QString::fromUtf8(ids[i]));

		auto *form = new QFormLayout();
		form->addRow(QString::fromUtf8(T_("UserProfile")), preset_);
		state_ = add_row(form, "Status.State");
		provider_ = add_row(form, "Status.Provider");
		unique_fps_ = add_row(form, "Status.UniqueFps");
		output_fps_ = add_row(form, "Status.OutputFps");
		repeated_ = add_row(form, "Status.Repeated");
		p95_ = add_row(form, "Status.P95");
		fallback_ = add_row(form, "Status.Fallback");
		root->addLayout(form);

		diagnostics_ = new QPlainTextEdit(this);
		diagnostics_->setReadOnly(true);
		diagnostics_->setMaximumBlockCount(200);
		root->addWidget(diagnostics_, 1);

		auto *notice = new QLabel(QString::fromUtf8(T_("Status.SyntheticNotice")), this);
		notice->setWordWrap(true);
		root->addWidget(notice);

		QObject::connect(start_, &QPushButton::clicked, this, [this] {
			if (auto c = current())
				c->start_session();
		});
		QObject::connect(stop_, &QPushButton::clicked, this, [this] {
			if (auto c = current())
				c->stop_session();
		});
		QObject::connect(pause_, &QPushButton::clicked, this, [this] {
			auto c = current();
			if (!c)
				return;
			paused_ = !paused_;
			if (paused_)
				c->pause();
			else
				c->resume();
		});
		QObject::connect(freeze_, &QPushButton::clicked, this, [this] {
			auto c = current();
			if (!c)
				return;
			frozen_ = !frozen_;
			c->freeze_neutral(frozen_);
		});
		QObject::connect(preset_, QOverload<int>::of(&QComboBox::activated), this,
				 [this](int) { apply_preset(); });

		timer_ = new QTimer(this);
		QObject::connect(timer_, &QTimer::timeout, this, [this] { refresh(); });
		timer_->start(1000);
		refresh();
	}

private:
	QLabel *add_row(QFormLayout *form, const char *key)
	{
		auto *l = new QLabel(QStringLiteral("-"), this);
		l->setTextInteractionFlags(Qt::TextSelectableByMouse);
		form->addRow(QString::fromUtf8(T_(key)), l);
		return l;
	}

	std::shared_ptr<EngineController> current()
	{
		const QString name = source_combo_->currentText();
		for (auto &e : entries_)
			if (QString::fromStdString(e.name) == name)
				return e.controller;
		return nullptr;
	}

	void apply_preset()
	{
		const QString name = source_combo_->currentText();
		for (auto &e : entries_) {
			if (QString::fromStdString(e.name) != name)
				continue;
			obs_source_t *src = obs_weak_source_get_source(e.weak);
			if (!src)
				return;
			obs_data_t *d = obs_data_create();
			obs_data_set_string(d, "user_profile", preset_->currentData().toString().toUtf8().constData());
			obs_source_update(src, d); // GUI thread, same as the properties dialog
			obs_data_release(d);
			obs_source_release(src);
			return;
		}
	}

	void refresh()
	{
		entries_ = registry_snapshot();
		const QString selected = source_combo_->currentText();
		source_combo_->blockSignals(true);
		source_combo_->clear();
		for (auto &e : entries_)
			source_combo_->addItem(QString::fromStdString(e.name));
		const int idx = source_combo_->findText(selected);
		if (idx >= 0)
			source_combo_->setCurrentIndex(idx);
		source_combo_->blockSignals(false);

		auto c = current();
		const bool have = c != nullptr;
		for (QPushButton *b : {start_, stop_, pause_, freeze_})
			b->setEnabled(have);
		if (!have) {
			state_->setText(QString::fromUtf8(T_("Dock.NoSource")));
			return;
		}
		ControllerStatus st = c->status();
		const json::Value &m = st.metrics;
		state_->setText(QString::fromStdString(st.state + (st.detail.empty() ? "" : " (" + st.detail + ")")));
		provider_->setText(QString::fromStdString(
			m["execution_provider_selected"].as_string(st.provider.empty() ? "-" : st.provider) + " / " +
			m["provider_device"].as_string("-")));
		unique_fps_->setText(num(m["face_swap_unique_fps"]));
		output_fps_->setText(num(m["output_fps"]));
		repeated_->setText(num(m["repeated_frame_pct"], " %"));
		p95_->setText(num(m["capture_to_output_p95_ms"], " ms", 0));
		fallback_->setText(st.fallback_reason.empty() ? QStringLiteral("-")
							      : QString::fromStdString(st.fallback_reason));

		const std::string diag = "state=" + st.state + " restarts=" + std::to_string(st.restarts) +
					 (st.last_error.empty() ? "" : " error=" + st.last_error);
		if (diag != last_diag_) {
			diagnostics_->appendPlainText(QString::fromStdString(diag));
			last_diag_ = diag;
		}
	}

	QComboBox *source_combo_ = nullptr;
	QComboBox *preset_ = nullptr;
	QPushButton *start_ = nullptr, *stop_ = nullptr, *pause_ = nullptr, *freeze_ = nullptr;
	QLabel *state_ = nullptr, *provider_ = nullptr, *unique_fps_ = nullptr, *output_fps_ = nullptr,
	       *repeated_ = nullptr, *p95_ = nullptr, *fallback_ = nullptr;
	QPlainTextEdit *diagnostics_ = nullptr;
	QTimer *timer_ = nullptr;
	std::vector<SourceEntry> entries_;
	std::string last_diag_;
	bool paused_ = false;
	bool frozen_ = false;
};

QPointer<AvatarDock> g_dock;

} // namespace

void dock_create()
{
	if (g_dock)
		return;
	auto *dock = new AvatarDock();
	// The frontend takes ownership of the widget.
	if (!obs_frontend_add_dock_by_id(kDockId, T_("Dock.Title"), dock)) {
		delete dock;
		return;
	}
	g_dock = dock;
}

void dock_destroy()
{
	if (g_dock) {
		obs_frontend_remove_dock(kDockId);
		g_dock = nullptr;
	}
}

} // namespace avatar
