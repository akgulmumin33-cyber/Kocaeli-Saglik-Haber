// Firebase web ayarları (bildirimler için).
// Firebase konsolu > Proje ayarları (⚙️) > Genel > "Uygulamalarınız" > Web uygulaması (</>) bölümündeki
// firebaseConfig değerlerini aşağıya yapıştırın. Bu bilgiler gizli değildir; web sayfalarında açıkça bulunur.
// "BURAYA" yazan alanlar doldurulmadıkça uygulama bildirimsiz çalışır.
self.APP_CONFIG = {
  firebase: {
    apiKey: "BURAYA",
    authDomain: "BURAYA.firebaseapp.com",
    projectId: "BURAYA",
    storageBucket: "BURAYA.appspot.com",
    messagingSenderId: "BURAYA",
    appId: "BURAYA"
  },
  // Firebase > Proje ayarları > Cloud Messaging > Web yapılandırması > Web Push sertifikaları > "Anahtar çifti"
  vapidKey: "BURAYA"
};
self.APP_CONFIG.pushReady = JSON.stringify(self.APP_CONFIG).indexOf("BURAYA") === -1;
