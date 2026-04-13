// Copyright 2019 The PDFium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "core/fxcrt/observed_ptr.h"

#include "core/fxcrt/check.h"
#include "core/fxcrt/containers/contains.h"

namespace fxcrt {

Observable::Observable() = default;

Observable::~Observable() {
  NotifyObservers();
}

void Observable::AddObserver(ObserverIface* pObserver) {
  std::lock_guard<std::mutex> lock(mutex_);
  DCHECK(!pdfium::Contains(observers_, pObserver));
  observers_.insert(pObserver);
}

void Observable::RemoveObserver(ObserverIface* pObserver) {
  std::lock_guard<std::mutex> lock(mutex_);
  DCHECK(pdfium::Contains(observers_, pObserver));
  observers_.erase(pObserver);
}

void Observable::NotifyObservers() {
  std::set<ObserverIface*> local_observers;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    local_observers.swap(observers_);
  }
  for (auto* pObserver : local_observers) {
    pObserver->OnObservableDestroyed();
  }
}

}  // namespace fxcrt
